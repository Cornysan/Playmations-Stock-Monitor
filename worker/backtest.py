#!/usr/bin/env python3
"""
backtest.py
===========
Backtester + Strategie-CLI. Wird vom Web-Prozess gespawnt (JSON auf stdout),
funktioniert aber genauso von Hand:

  python backtest.py list                                    Strategien + Params-Schema
  python backtest.py run SYMBOL --strategy NAME [--params JSON] [--db PATH]
                     [--timeframe 1d|1h]

Ausführungsmodell (--execution):
  next_open (Default)  Signal am Close von Bar t → Fill zur Eröffnung von t+1.
                       Entspricht dem Live-Trading (Worker läuft nach US-Close,
                       Alpaca queued Market-Orders zur nächsten Eröffnung).
  close                Fill zum Signal-Close selbst (optimistisch, alter Modus).
Eine Position, all-in/all-out, keine Gebühren/Slippage (Alpaca: kommissionsfrei).
Die Macro-Säule liegt historisch nicht vor — Strategien laufen hier mit
macro_score=None.

stdlib only, DB strikt read-only (mode=ro), importiert kein fetch/yfinance.
"""
from __future__ import annotations
import argparse
import json
import sqlite3
import sys

import config
import strategies

WARMUP = 60  # gleiche Mindesthistorie wie main.analyze_symbols


def load_bars(db_path, symbol: str, timeframe: str = "1d"):
    """(dates, opens, highs, lows, closes) — dates sind ISO-Tage (1d) bzw.
    ISO-UTC-Datetimes (1h), beides sortiert lexikographisch korrekt."""
    sql = ("SELECT ts, open, high, low, close FROM bars_1h "
           "WHERE symbol=? AND close IS NOT NULL ORDER BY ts") if timeframe == "1h" else \
          ("SELECT date, open, high, low, close FROM bars "
           "WHERE symbol=? AND close IS NOT NULL ORDER BY date")
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = con.execute(sql, (symbol,)).fetchall()
    finally:
        con.close()
    return ([r[0] for r in rows], [r[1] for r in rows], [r[2] for r in rows],
            [r[3] for r in rows], [r[4] for r in rows])


def run_backtest(dates: list[str], opens: list, highs: list, lows: list,
                 closes: list[float], strat, params: dict,
                 execution: str = "next_open", risk: dict | None = None) -> dict:
    n = len(closes)
    signals: list[dict] = []
    trades: list[dict] = []
    equity: list[dict] = []

    # Risk-Overlay (unabhängig von der Signal-Strategie): fester Stop unterm
    # Einstieg und/oder Trailing-Stop unterm Hoch seit Einstieg. Simulation
    # intra-Kerze gegen Open/Low; das Level stammt immer aus dem Stand VOR der
    # aktuellen Kerze (kein Blick in die laufende Kerze beim Nachziehen).
    risk = risk or {}
    stop_pct = risk.get("stop_loss_pct")
    trail_pct = risk.get("trail_pct")
    has_risk = bool(stop_pct or trail_pct)

    in_pos = False
    entry_date, entry_price, entry_idx = None, None, None
    pos_peak = None          # Hoch seit Einstieg (Basis des Trailing-Stops)
    eq, peak, max_dd = 1.0, 1.0, 0.0
    bars_in_market = 0
    pending = None  # next_open: gestriges Signal, wird an der heutigen Eröffnung gefüllt

    def open_or_close(i: int) -> float:
        return opens[i] if opens[i] is not None else closes[i]

    def stop_level() -> tuple[float, str] | None:
        """(Level, Grund) des aktuell schärfsten Stops, oder None."""
        if not in_pos or not has_risk:
            return None
        best = None
        if stop_pct:
            best = (entry_price * (1 - stop_pct / 100.0), "stop")
        if trail_pct:
            t = (pos_peak * (1 - trail_pct / 100.0), "trail")
            if best is None or t[0] > best[0]:
                best = t
        return best

    def close_trade(i: int, px: float, reason: str) -> None:
        nonlocal in_pos, pos_peak
        in_pos = False
        pos_peak = None
        trades.append({
            "entry_date": entry_date, "entry_price": entry_price,
            "exit_date": dates[i], "exit_price": px,
            "pnl_pct": round((px / entry_price - 1) * 100, 2),
            "bars_held": i - entry_idx,
            "open": False,
            "exit_reason": reason,
        })
        sig = {"date": dates[i], "type": "sell", "price": px}
        if reason != "signal":
            sig["reason"] = reason
        signals.append(sig)

    for i in range(WARMUP, n):
        entered_at_open = False
        ref_close = closes[i - 1]  # Equity-Referenz für Positionen aus der Vor-Kerze

        # 1. next_open: gestriges Signal zur heutigen Eröffnung ausführen
        if pending == "buy" and not in_pos:
            px = open_or_close(i)
            in_pos, entered_at_open = True, True
            entry_date, entry_price, entry_idx = dates[i], px, i
            pos_peak = px
            signals.append({"date": dates[i], "type": "buy", "price": px})
        elif pending == "sell" and in_pos:
            px = open_or_close(i)
            eq *= px / ref_close
            close_trade(i, px, "signal")
        pending = None

        # 2. Stop-Check intra-Kerze: Gap unter dem Level → Fill zur Eröffnung,
        #    sonst Low unter dem Level → Fill am Level.
        stopped_px = None
        if in_pos and has_risk and (lvl := stop_level()) is not None:
            level, reason = lvl
            o = open_or_close(i)
            low = lows[i] if lows[i] is not None else closes[i]
            if not entered_at_open and o <= level:
                stopped_px = o        # Gap: Eröffnung liegt schon unterm Level
            elif low <= level:
                stopped_px = level
            if stopped_px is not None:
                eq *= stopped_px / (entry_price if entered_at_open else ref_close)
                bars_in_market += 1
                close_trade(i, round(stopped_px, 4), reason)

        # 3. Equity über den heutigen Bar fortschreiben
        if stopped_px is None:
            if entered_at_open:
                eq *= closes[i] / entry_price
                bars_in_market += 1
            elif in_pos:
                eq *= closes[i] / ref_close
                bars_in_market += 1

        # 4. Entscheidung auf dem heutigen Close
        card = strategies.run(strat, closes[: i + 1], holding=in_pos, params=params)
        sig = card["signal"]

        # 5. Ausführung: sofort (close) oder morgen früh (next_open)
        if execution == "close":
            if sig == "BUY" and not in_pos:
                in_pos = True
                entry_date, entry_price, entry_idx = dates[i], closes[i], i
                pos_peak = closes[i]
                signals.append({"date": dates[i], "type": "buy", "price": closes[i]})
            elif sig == "SELL" and in_pos:
                # Equity ist in Schritt 3 bereits bis zum Close fortgeschrieben
                close_trade(i, closes[i], "signal")
        else:
            if (sig == "BUY" and not in_pos) or (sig == "SELL" and in_pos):
                pending = sig.lower()

        # 6. Trailing-Basis erst NACH allen Checks mit dem heutigen Hoch nachziehen
        if in_pos and trail_pct:
            high = highs[i] if highs[i] is not None else closes[i]
            pos_peak = max(pos_peak, high)

        peak = max(peak, eq)
        max_dd = max(max_dd, 1.0 - eq / peak)
        equity.append({"date": dates[i], "value": round(eq, 6)})

    # next_open: Signal vom letzten Bar wartet noch auf seine Ausführung
    if pending is not None:
        signals.append({"date": dates[-1], "type": pending, "price": None,
                        "pending": True})

    if in_pos:  # offene Position mark-to-market
        trades.append({
            "entry_date": entry_date, "entry_price": entry_price,
            "exit_date": None, "exit_price": closes[-1],
            "pnl_pct": round((closes[-1] / entry_price - 1) * 100, 2),
            "bars_held": n - 1 - entry_idx,
            "open": True,
            "exit_reason": None,
        })

    closed = [t for t in trades if not t["open"]]
    wins = [t for t in closed if t["pnl_pct"] > 0]
    losses = [t for t in closed if t["pnl_pct"] <= 0]
    gross_win = sum(t["pnl_pct"] for t in wins)
    gross_loss = -sum(t["pnl_pct"] for t in losses)

    def pct(x):
        return round(x, 2) if x is not None else None

    stats = {
        "total_return_pct": pct((eq - 1) * 100),
        "buy_hold_return_pct": pct((closes[-1] / closes[WARMUP] - 1) * 100),
        "n_trades": len(trades),
        "n_closed": len(closed),
        "win_rate_pct": pct(len(wins) / len(closed) * 100) if closed else None,
        "avg_win_pct": pct(gross_win / len(wins)) if wins else None,
        "avg_loss_pct": pct(-gross_loss / len(losses)) if losses else None,
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else None,
        "max_drawdown_pct": pct(max_dd * 100),
        "exposure_pct": pct(bars_in_market / (n - WARMUP) * 100) if n > WARMUP else None,
        "n_stop_exits": len([t for t in closed
                             if t.get("exit_reason") in ("stop", "trail")]),
    }
    return {"signals": signals, "trades": trades, "stats": stats, "equity": equity}


def parse_risk(raw: str | None) -> dict:
    """Risk-Overlay validieren: nur stop_loss_pct / trail_pct, je 0.1–50 %.
    Ungültige Angaben lösen ValueError aus (Fehler statt stiller Ignoranz)."""
    if not raw:
        return {}
    risk = json.loads(raw)
    if not isinstance(risk, dict):
        raise ValueError("risk must be a JSON object")
    out = {}
    for key in ("stop_loss_pct", "trail_pct"):
        v = risk.get(key)
        if v is None:
            continue
        if not isinstance(v, (int, float)) or not 0.1 <= float(v) <= 50:
            raise ValueError(f"{key} must be a number between 0.1 and 50")
        out[key] = float(v)
    unknown = set(risk) - {"stop_loss_pct", "trail_pct"}
    if unknown:
        raise ValueError(f"unknown risk keys: {sorted(unknown)}")
    return out


def cmd_run(args) -> int:
    strat = strategies.get(args.strategy)
    if strat is None:
        print(json.dumps({"error": f"unbekannte Strategie {args.strategy!r}"}))
        return 1
    try:
        params = json.loads(args.params) if args.params else {}
        if not isinstance(params, dict):
            raise ValueError("params must be a JSON object")
    except ValueError as e:
        print(json.dumps({"error": f"ungültige Params: {e}"}))
        return 1
    try:
        risk = parse_risk(args.risk)
    except ValueError as e:
        print(json.dumps({"error": f"ungültiges Risk-Overlay: {e}"}))
        return 1

    symbol = args.symbol.upper()
    dates, opens, highs, lows, closes = load_bars(
        args.db or config.DB_PATH, symbol, args.timeframe)
    if len(closes) <= WARMUP:
        print(json.dumps({"error": f"zu wenig Bars für {symbol} "
                                   f"({len(closes)}, benötigt >{WARMUP})"}))
        return 1

    resolved = strategies.resolve_params(strat, params)
    result = run_backtest(dates, opens, highs, lows, closes, strat, resolved,
                          args.execution, risk)
    result["meta"] = {
        "symbol": symbol,
        "strategy": strat.NAME,
        "params": resolved,
        "from": dates[WARMUP],
        "to": dates[-1],
        "n_bars": len(closes),
        "execution": args.execution,
        "timeframe": args.timeframe,
        "risk": risk or None,
        "macro": None,
    }
    print(json.dumps(result, ensure_ascii=False))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Strategy backtester (JSON output).")
    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="Strategien + Params-Schemata")
    run_p = sub.add_parser("run", help="Backtest für ein Symbol")
    run_p.add_argument("symbol")
    run_p.add_argument("--strategy", default=strategies.DEFAULT)
    run_p.add_argument("--params", help="JSON-Objekt mit Param-Overrides")
    run_p.add_argument("--execution", choices=["next_open", "close"],
                       default="next_open")
    run_p.add_argument("--timeframe", choices=["1d", "1h"], default="1d",
                       help="1h testet auf Stundenkerzen (Tabelle bars_1h)")
    run_p.add_argument("--risk", help='Risk-Overlay, z. B. {"trail_pct": 3} — '
                                      "Stops werden intra-Kerze gegen Open/Low simuliert")
    run_p.add_argument("--db", help="Pfad zur SQLite-DB (Default: config.DB_PATH)")
    args = ap.parse_args()

    if args.command == "list":
        print(json.dumps(strategies.list_all(), ensure_ascii=False))
        return 0
    return cmd_run(args)


if __name__ == "__main__":
    raise SystemExit(main())
