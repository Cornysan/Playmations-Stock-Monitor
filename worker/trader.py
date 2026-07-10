#!/usr/bin/env python3
"""
trader.py
=========
Auto-Trading-Executor (Pott-Modell): Kapital wird gleichmäßig auf die
Auto-Trade-Symbole verteilt — Pott = Equity / N. Pro Symbol all-in/all-out,
gesteuert vom normalisierten Signal der aktiven Strategie (analysis.signal).

Ablauf pro Tagesrun (main.run_once):
  sync(con)          VOR der Analyse: Order-Status refreshen, watchlist.holding
                     aus den echten Alpaca-Positionen setzen, Konto-Snapshot.
  trade(con, as_of)  NACH der Analyse: BUY im Flat → Notional-Kauf (Pott),
                     SELL in Position → Market-Sell der ganzen Position.

Sicherungen: TRADING_ENABLED-Opt-in, client_order_id-Idempotenz (pro Symbol,
Tag und Seite), MAX_ORDERS_PER_RUN-Circuit-Breaker, Fehler einzelner Orders
brechen weder den Run noch die restlichen Orders.
"""
from __future__ import annotations
import datetime as dt
import logging

import broker
import config
import db

log = logging.getLogger("trader")

MIN_NOTIONAL = 1.0  # Alpaca-Minimum für Notional-Orders


def enabled() -> bool:
    return config.TRADING_ENABLED and broker.configured()


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _order_row(alpaca: dict | None, cid: str, symbol: str, side: str,
               notional: float | None = None, error: str | None = None) -> dict:
    a = alpaca or {}
    return {
        "client_order_id": cid,
        "alpaca_id": a.get("id"),
        "symbol": symbol,
        "side": side,
        "notional": notional,
        "submitted_at": a.get("submitted_at") or now_iso(),
        "status": "error" if error else a.get("status", "unknown"),
        "filled_qty": float(a["filled_qty"]) if a.get("filled_qty") else None,
        "filled_avg_price": float(a["filled_avg_price"]) if a.get("filled_avg_price") else None,
        "filled_at": a.get("filled_at"),
        "error": error,
    }


# ---------------------------------------------------------------------------
# Phase 1: sync (vor der Analyse)
# ---------------------------------------------------------------------------

def sync(con) -> None:
    # 1. Nicht-finale Orders gegen Alpaca refreshen (Fills der letzten Eröffnung)
    for row in db.open_orders(con):
        try:
            o = broker.get_order_by_client_id(row["client_order_id"])
        except broker.BrokerError as e:
            log.warning("order refresh %s failed: %s", row["client_order_id"], e)
            continue
        if o is not None:
            db.upsert_order(con, _order_row(
                o, row["client_order_id"], row["symbol"], row["side"],
                row["notional"]))

    # 2. holding der Auto-Trade-Symbole = echte Alpaca-Position (Quelle der Wahrheit).
    #    Der manuelle Toggle bleibt für alle übrigen Symbole maßgeblich.
    positions = broker.get_positions()
    for sym in db.autotrade_symbols(con):
        con.execute("UPDATE watchlist SET holding=? WHERE symbol=?",
                    (1 if sym in positions else 0, sym))
    con.commit()

    # 3. Konto-Snapshot für UI/Verlauf
    acct = broker.get_account()
    db.write_broker_snapshot(con, now_iso(), acct["equity"], acct["cash"],
                             acct["buying_power"], positions)
    log.info("broker sync: equity=%.2f cash=%.2f positions=%d",
             acct["equity"], acct["cash"], len(positions))


# ---------------------------------------------------------------------------
# Phase 2: trade (nach der Analyse)
# ---------------------------------------------------------------------------

def trade(con, as_of: str) -> None:
    symbols = db.autotrade_symbols(con)
    if not symbols:
        log.info("no autotrade symbols — nothing to do")
        return

    acct = broker.get_account()
    if acct.get("trading_blocked"):
        log.error("account is trading_blocked — skipping all orders")
        return
    positions = broker.get_positions()
    pot = acct["equity"] / len(symbols)
    cash = acct["cash"]
    day = as_of[:10].replace("-", "")
    log.info("trade: %d symbols, pot=%.2f (equity %.2f / %d), cash=%.2f",
             len(symbols), pot, acct["equity"], len(symbols), cash)

    placeholders = ",".join("?" * len(symbols))
    signals = {r["symbol"]: r["signal"] for r in con.execute(
        f"SELECT symbol, signal FROM analysis WHERE as_of=? AND symbol IN ({placeholders})",
        (as_of, *symbols))}

    n_orders = 0
    for sym in symbols:
        sig = signals.get(sym)
        if sig is None:
            log.warning("%s: no analysis for this run — skipped", sym)
            continue
        if n_orders >= config.MAX_ORDERS_PER_RUN:
            log.error("MAX_ORDERS_PER_RUN (%d) reached — remaining symbols skipped",
                      config.MAX_ORDERS_PER_RUN)
            break

        if sig == "BUY" and sym not in positions:
            cid = f"spm-{sym}-{day}-buy"
            if db.order_exists(con, cid):
                log.info("%s: buy already submitted today (%s)", sym, cid)
                continue
            notional = round(min(pot, cash), 2)
            if notional < max(MIN_NOTIONAL, 0.01 * pot):
                log.warning("%s: cash %.2f zu klein für Pott %.2f — skipped",
                            sym, cash, pot)
                db.upsert_order(con, _order_row(
                    None, cid, sym, "buy", notional,
                    error=f"insufficient cash ({cash:.2f} for pot {pot:.2f})"))
                continue
            try:
                o = broker.submit_notional_buy(sym, notional, cid)
                db.upsert_order(con, _order_row(o, cid, sym, "buy", notional))
                cash -= notional
                n_orders += 1
                log.info("%s: BUY %.2f USD submitted (%s)", sym, notional, o.get("status"))
            except broker.BrokerError as e:
                db.upsert_order(con, _order_row(None, cid, sym, "buy", notional,
                                                error=str(e)))
                log.error("%s: buy failed: %s", sym, e)

        elif sig == "SELL" and sym in positions:
            cid = f"spm-{sym}-{day}-sell"
            if db.order_exists(con, cid):
                log.info("%s: sell already submitted today (%s)", sym, cid)
                continue
            qty = positions[sym]["qty_raw"]
            try:
                o = broker.submit_market_sell(sym, qty, cid)
                db.upsert_order(con, _order_row(o, cid, sym, "sell"))
                n_orders += 1
                log.info("%s: SELL %s Stk. submitted (%s)", sym, qty, o.get("status"))
            except broker.BrokerError as e:
                db.upsert_order(con, _order_row(None, cid, sym, "sell", error=str(e)))
                log.error("%s: sell failed: %s", sym, e)

    log.info("trade complete: %d order(s) submitted", n_orders)
