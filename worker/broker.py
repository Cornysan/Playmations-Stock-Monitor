#!/usr/bin/env python3
"""
broker.py
=========
Dünner Alpaca-REST-Client (Trading-API v2). Standard: Paper-Endpoint —
Live erfordert explizit ALPACA_BASE_URL=https://api.alpaca.markets.

Keine Order-Logik hier (die liegt in trader.py), nur HTTP + Fehlerbehandlung.
Secrets erscheinen nie in Logs oder Exceptions.

CLI zum Testen:
  python broker.py account     Konto (equity, cash, buying power)
  python broker.py positions   offene Positionen
  python broker.py orders      letzte Orders (alle Status)
  python broker.py clock       Marktzeit / offen?
"""
from __future__ import annotations
import json
import logging
import sys

import requests

import config

log = logging.getLogger("broker")


class BrokerError(Exception):
    """Alpaca-Fehler (HTTP != 2xx oder Netzwerk). message ist log-sicher."""


def configured() -> bool:
    return bool(config.ALPACA_KEY_ID and config.ALPACA_SECRET_KEY)


def _request(method: str, path: str, payload: dict | None = None,
             params: dict | None = None):
    if not configured():
        raise BrokerError("ALPACA_KEY_ID/ALPACA_SECRET_KEY nicht konfiguriert")
    url = config.ALPACA_BASE_URL.rstrip("/") + path
    headers = {
        "APCA-API-KEY-ID": config.ALPACA_KEY_ID,
        "APCA-API-SECRET-KEY": config.ALPACA_SECRET_KEY,
    }
    try:
        resp = requests.request(method, url, headers=headers, json=payload,
                                params=params, timeout=20)
    except requests.RequestException as e:
        raise BrokerError(f"{method} {path}: {type(e).__name__}") from e
    if not resp.ok:
        # Alpaca liefert {"code":…, "message":…} — Message ist secret-frei
        try:
            detail = resp.json().get("message", resp.text[:200])
        except ValueError:
            detail = resp.text[:200]
        raise BrokerError(f"{method} {path}: HTTP {resp.status_code} — {detail}")
    return resp.json() if resp.text else None


def get_account() -> dict:
    """equity/cash/buying_power als float, plus status & currency."""
    a = _request("GET", "/v2/account")
    return {
        "equity": float(a["equity"]),
        "cash": float(a["cash"]),
        "buying_power": float(a["buying_power"]),
        "currency": a.get("currency", "USD"),
        "status": a.get("status"),
        "trading_blocked": a.get("trading_blocked", False),
    }


def get_positions() -> dict[str, dict]:
    """{symbol: {qty, market_value, avg_entry_price, unrealized_plpc}}"""
    out = {}
    for p in _request("GET", "/v2/positions") or []:
        out[p["symbol"]] = {
            "qty": float(p["qty"]),
            "qty_raw": p["qty"],  # exakter String für all-out-Verkäufe
            "market_value": float(p["market_value"]),
            "avg_entry_price": float(p["avg_entry_price"]),
            "unrealized_plpc": float(p.get("unrealized_plpc") or 0.0),
        }
    return out


def submit_notional_buy(symbol: str, notional: float, client_order_id: str) -> dict:
    """Market-Buy über Dollar-Betrag (Bruchstücke). Nach US-Close eingereicht
    queued Alpaca die Order automatisch zur nächsten Markteröffnung."""
    return _request("POST", "/v2/orders", payload={
        "symbol": symbol,
        "notional": str(round(notional, 2)),
        "side": "buy",
        "type": "market",
        "time_in_force": "day",
        "client_order_id": client_order_id,
    })


def submit_market_sell(symbol: str, qty: str, client_order_id: str) -> dict:
    """Market-Sell einer konkreten Stückzahl (qty als String, Bruchstücke ok).
    Bewusst statt DELETE /v2/positions, damit die client_order_id-Idempotenz
    auch für Verkäufe greift."""
    return _request("POST", "/v2/orders", payload={
        "symbol": symbol,
        "qty": qty,
        "side": "sell",
        "type": "market",
        "time_in_force": "day",
        "client_order_id": client_order_id,
    })


def submit_qty_buy(symbol: str, qty: int, client_order_id: str) -> dict:
    """Market-Buy ganzer Stückzahlen. Für Positionen mit Stop-Schutz Pflicht:
    Alpaca erlaubt Stop-/Trailing-Orders nur auf ganze Aktien."""
    return _request("POST", "/v2/orders", payload={
        "symbol": symbol,
        "qty": str(int(qty)),
        "side": "buy",
        "type": "market",
        "time_in_force": "day",
        "client_order_id": client_order_id,
    })


def submit_trailing_stop(symbol: str, qty: int, trail_percent: float,
                         client_order_id: str) -> dict:
    """Trailing-Stop-Sell (GTC): Alpaca zieht das Stop-Level am Hoch nach und
    verkauft intra-Kerze — unabhängig von den Worker-Läufen."""
    return _request("POST", "/v2/orders", payload={
        "symbol": symbol,
        "qty": str(int(qty)),
        "side": "sell",
        "type": "trailing_stop",
        "trail_percent": str(round(trail_percent, 2)),
        "time_in_force": "gtc",
        "client_order_id": client_order_id,
    })


def submit_stop_sell(symbol: str, qty: int, stop_price: float,
                     client_order_id: str) -> dict:
    """Fester Stop-Loss-Sell (GTC) zu einem absoluten Stop-Preis."""
    return _request("POST", "/v2/orders", payload={
        "symbol": symbol,
        "qty": str(int(qty)),
        "side": "sell",
        "type": "stop",
        "stop_price": str(round(stop_price, 2)),
        "time_in_force": "gtc",
        "client_order_id": client_order_id,
    })


def cancel_order(order_id: str) -> None:
    """Order stornieren; bereits gefüllte/stornierte Orders sind kein Fehler."""
    try:
        _request("DELETE", f"/v2/orders/{order_id}")
    except BrokerError as e:
        if "404" in str(e) or "422" in str(e):
            return
        raise


def get_order_by_client_id(client_order_id: str) -> dict | None:
    try:
        return _request("GET", "/v2/orders:by_client_order_id",
                        params={"client_order_id": client_order_id})
    except BrokerError as e:
        if "404" in str(e):
            return None
        raise


def get_orders(status: str = "all", limit: int = 50) -> list[dict]:
    return _request("GET", "/v2/orders",
                    params={"status": status, "limit": limit, "direction": "desc"}) or []


def get_clock() -> dict:
    return _request("GET", "/v2/clock")


# ---------------------------------------------------------------------------

def main() -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stdout)
    cmd = sys.argv[1] if len(sys.argv) > 1 else "account"
    try:
        if cmd == "account":
            print(json.dumps(get_account(), indent=2))
        elif cmd == "positions":
            print(json.dumps(get_positions(), indent=2))
        elif cmd == "orders":
            print(json.dumps(get_orders(), indent=2))
        elif cmd == "clock":
            print(json.dumps(get_clock(), indent=2))
        else:
            print("usage: broker.py account|positions|orders|clock", file=sys.stderr)
            return 2
    except BrokerError as e:
        print(f"BrokerError: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
