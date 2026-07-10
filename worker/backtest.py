#!/usr/bin/env python3
"""
backtest.py
===========
Backtester + Strategie-CLI. Wird vom Web-Prozess gespawnt (JSON auf stdout),
funktioniert aber genauso von Hand:

  python backtest.py list                                    Strategien + Params-Schema
  python backtest.py run SYMBOL --strategy NAME [--params JSON] [--db PATH]

Ausführungsmodell: Signal entsteht am Close von Bar t und wird zum SELBEN
Close gefüllt. Eine Position, all-in/all-out, keine Gebühren/Slippage.
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


def load_bars(db_path, symbol: str) -> tuple[list[str], list[float]]:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT date, close FROM bars WHERE symbol=? AND close IS NOT NULL "
            "ORDER BY date",
            (symbol,),
        ).fetchall()
    finally:
        con.close()
    return [r[0] for r in rows], [r[1] for r in rows]


def run_backtest(dates: list[str], closes: list[float], strat, params: dict) -> dict:
    n = len(closes)
    signals: list[dict] = []
    trades: list[dict] = []
    equity: list[dict] = []

    in_pos = False
    entry_date, entry_price, entry_idx = None, None, None
    eq, peak, max_dd = 1.0, 1.0, 0.0
    bars_in_market = 0

    for i in range(WARMUP, n):
        # Position von gestern über den heutigen Bar tragen (Entscheidung folgt danach)
        if in_pos:
            eq *= closes[i] / closes[i - 1]
            bars_in_market += 1

        card = strategies.run(strat, closes[: i + 1], holding=in_pos, params=params)
        sig = card["signal"]

        if sig == "BUY" and not in_pos:
            in_pos = True
            entry_date, entry_price, entry_idx = dates[i], closes[i], i
            signals.append({"date": dates[i], "type": "buy", "price": closes[i]})
        elif sig == "SELL" and in_pos:
            in_pos = False
            trades.append({
                "entry_date": entry_date, "entry_price": entry_price,
                "exit_date": dates[i], "exit_price": closes[i],
                "pnl_pct": round((closes[i] / entry_price - 1) * 100, 2),
                "bars_held": i - entry_idx,
                "open": False,
            })
            signals.append({"date": dates[i], "type": "sell", "price": closes[i]})

        peak = max(peak, eq)
        max_dd = max(max_dd, 1.0 - eq / peak)
        equity.append({"date": dates[i], "value": round(eq, 6)})

    if in_pos:  # offene Position mark-to-market
        trades.append({
            "entry_date": entry_date, "entry_price": entry_price,
            "exit_date": None, "exit_price": closes[-1],
            "pnl_pct": round((closes[-1] / entry_price - 1) * 100, 2),
            "bars_held": n - 1 - entry_idx,
            "open": True,
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
    }
    return {"signals": signals, "trades": trades, "stats": stats, "equity": equity}


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

    symbol = args.symbol.upper()
    dates, closes = load_bars(args.db or config.DB_PATH, symbol)
    if len(closes) <= WARMUP:
        print(json.dumps({"error": f"zu wenig Bars für {symbol} "
                                   f"({len(closes)}, benötigt >{WARMUP})"}))
        return 1

    resolved = strategies.resolve_params(strat, params)
    result = run_backtest(dates, closes, strat, resolved)
    result["meta"] = {
        "symbol": symbol,
        "strategy": strat.NAME,
        "params": resolved,
        "from": dates[WARMUP],
        "to": dates[-1],
        "n_bars": len(closes),
        "execution": "signal close",
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
    run_p.add_argument("--db", help="Pfad zur SQLite-DB (Default: config.DB_PATH)")
    args = ap.parse_args()

    if args.command == "list":
        print(json.dumps(strategies.list_all(), ensure_ascii=False))
        return 0
    return cmd_run(args)


if __name__ == "__main__":
    raise SystemExit(main())
