#!/usr/bin/env python3
"""
backtest.py
===========
Backtester + Strategie-CLI. Wird vom Web-Prozess gespawnt (JSON auf stdout),
funktioniert aber genauso von Hand:

  python backtest.py list                                    Strategien + Params-Schema
  python backtest.py run SYMBOL --strategy NAME [--params JSON] [--db PATH]
                     [--timeframe 1d|1h]
  python backtest.py portfolio [--symbols A,B,C] …           Pott-Modell über
                     mehrere Symbole (Default: die Watchlist aus der DB)
  python backtest.py sweep SYMBOL [--split 0.7] [--grid JSON] …
                     Parameter-Sweep mit Train/Test-Split: Grid optimieren auf
                     dem Trainings-Teil, blind auswerten auf dem Test-Teil

Ausführungsmodell (--execution):
  next_open (Default)  Signal am Close von Bar t → Fill zur Eröffnung von t+1.
                       Entspricht dem Live-Trading (Worker läuft nach US-Close,
                       Alpaca queued Market-Orders zur nächsten Eröffnung).
  close                Fill zum Signal-Close selbst (optimistisch, alter Modus).
Eine Position je Symbol, all-in/all-out. Alpaca ist kommissionsfrei, aber
Market-Orders zahlen Spread + Slippage: --slippage-bps (Default 5 = 0,05 %)
verschlechtert jeden Fill um diesen Satz (Käufe teurer, Verkäufe billiger);
auch der Buy&Hold-Vergleich zahlt seinen Einstiegs-Fill.
Die Macro-Säule liegt historisch nicht vor — Strategien laufen hier mit
macro_score=None.

stdlib only, DB strikt read-only (mode=ro), importiert kein fetch/yfinance.
"""
from __future__ import annotations
import argparse
import itertools
import json
import sqlite3
import sys

import config
import strategies

WARMUP = 60  # gleiche Mindesthistorie wie main.analyze_symbols
MAX_SWEEP_COMBOS = 500  # Schutz gegen explodierende Grids (→ --grid eingrenzen)


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
                 execution: str = "next_open", risk: dict | None = None,
                 slippage_bps: float = 0.0) -> dict:
    n = len(closes)
    slip = (slippage_bps or 0.0) / 10000.0  # je Fill-Seite: Buy × (1+slip), Sell × (1−slip)
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
            base = open_or_close(i)
            px = round(base * (1 + slip), 4)
            in_pos, entered_at_open = True, True
            entry_date, entry_price, entry_idx = dates[i], px, i
            pos_peak = base  # Trailing-Basis bleibt der Marktkurs, nicht der Fill
            signals.append({"date": dates[i], "type": "buy", "price": px})
        elif pending == "sell" and in_pos:
            px = round(open_or_close(i) * (1 - slip), 4)
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
                stopped_px = round(stopped_px * (1 - slip), 4)
                eq *= stopped_px / (entry_price if entered_at_open else ref_close)
                bars_in_market += 1
                close_trade(i, stopped_px, reason)

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
                px = round(closes[i] * (1 + slip), 4)
                entry_date, entry_price, entry_idx = dates[i], px, i
                pos_peak = closes[i]
                # Equity-Kette läuft über Closes → Slippage-Kosten sofort verbuchen
                eq *= closes[i] / px
                signals.append({"date": dates[i], "type": "buy", "price": px})
            elif sig == "SELL" and in_pos:
                # Equity ist in Schritt 3 bereits bis zum Close fortgeschrieben
                px = round(closes[i] * (1 - slip), 4)
                eq *= px / closes[i]
                close_trade(i, px, "signal")
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
        # fairer Vergleich: auch Buy&Hold zahlt seinen Einstiegs-Fill
        "buy_hold_return_pct": pct((closes[-1] / (closes[WARMUP] * (1 + slip)) - 1) * 100),
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


def run_portfolio(all_bars: dict[str, tuple], strat, params: dict,
                  risk: dict | None = None, slippage_bps: float = 0.0) -> dict:
    """Pott-Modell wie trader.py: ein gemeinsamer Cash-Topf, Pott = Equity / N,
    all-in/all-out je Symbol, Ausführung immer zur nächsten eigenen Eröffnung.
    Start-Equity ist auf 1.0 normiert; Buys investieren min(Pott, Cash)."""
    risk = risk or {}
    stop_pct = risk.get("stop_loss_pct")
    trail_pct = risk.get("trail_pct")
    slip = (slippage_bps or 0.0) / 10000.0

    syms = sorted(all_bars)
    n = len(syms)
    st: dict[str, dict] = {}
    for s in syms:
        dates, opens, highs, lows, closes = all_bars[s]
        st[s] = {
            "dates": dates, "opens": opens, "highs": highs, "lows": lows,
            "closes": closes, "idx": {d: i for i, d in enumerate(dates)},
            "qty": 0.0, "entry_price": None, "entry_date": None, "entry_bar": None,
            "pos_peak": None, "pending": None, "contrib": 0.0,
            "last_px": None,  # letzter bekannter Kurs — Mark für Equity/Pott
        }

    def open_px(x, i):
        return x["opens"][i] if x["opens"][i] is not None else x["closes"][i]

    cash = 1.0
    trades: list[dict] = []
    equity: list[dict] = []
    peak, max_dd = 1.0, 0.0
    invested_sum, invested_n = 0.0, 0

    def mark_equity() -> float:
        return cash + sum(x["qty"] * x["last_px"] for x in st.values()
                          if x["qty"] and x["last_px"] is not None)

    def close_pos(s: str, x: dict, date: str, i: int, px: float, reason: str) -> None:
        nonlocal cash
        px = round(px, 4)
        cash += x["qty"] * px
        x["contrib"] += x["qty"] * (px - x["entry_price"])
        trades.append({
            "symbol": s,
            "entry_date": x["entry_date"], "entry_price": x["entry_price"],
            "exit_date": date, "exit_price": px,
            "pnl_pct": round((px / x["entry_price"] - 1) * 100, 2),
            "bars_held": i - x["entry_bar"],
            "open": False, "exit_reason": reason,
        })
        x["qty"], x["entry_price"], x["pos_peak"] = 0.0, None, None

    timeline = sorted({d for s in syms for d in all_bars[s][0]})
    # Equity-Kurve erst ab dem Punkt, an dem das erste Symbol handeln darf
    start = min(x["dates"][WARMUP] for x in st.values())

    for date in timeline:
        active = [(s, st[s]["idx"][date]) for s in syms if date in st[s]["idx"]]
        entered: set[str] = set()

        # 0. Marks auf die heutige Eröffnung — Basis für Pott-Sizing (wie das
        #    Konto-Equity zum Zeitpunkt der Order im Live-Trading)
        for s, i in active:
            st[s]["last_px"] = open_px(st[s], i)

        # 1a. Pending-Sells zur Eröffnung füllen (Cash wird für Buys frei)
        for s, i in active:
            x = st[s]
            if x["pending"] == "sell":
                x["pending"] = None
                if x["qty"]:
                    close_pos(s, x, date, i, open_px(x, i) * (1 - slip), "signal")

        # 1b. Pending-Buys zur Eröffnung füllen — Pott = Equity/N, gedeckelt aufs Cash
        for s, i in active:
            x = st[s]
            if x["pending"] == "buy":
                x["pending"] = None
                if x["qty"]:
                    continue
                pot = mark_equity() / n
                budget = min(pot, cash)
                if budget < 0.01 * pot:  # Spiegel des Cash-Checks in trader.trade
                    continue
                base = open_px(x, i)
                px = base * (1 + slip)
                x["qty"] = budget / px
                cash -= budget
                x["entry_price"] = round(px, 4)
                x["entry_date"], x["entry_bar"] = date, i
                x["pos_peak"] = base
                entered.add(s)

        # 2. Stop-Check intra-Kerze (Level aus dem Stand VOR der Kerze)
        for s, i in active:
            x = st[s]
            if not x["qty"] or not (stop_pct or trail_pct):
                continue
            best = None
            if stop_pct:
                best = (x["entry_price"] * (1 - stop_pct / 100.0), "stop")
            if trail_pct and x["pos_peak"] is not None:
                t = (x["pos_peak"] * (1 - trail_pct / 100.0), "trail")
                if best is None or t[0] > best[0]:
                    best = t
            level, reason = best
            o = open_px(x, i)
            low = x["lows"][i] if x["lows"][i] is not None else x["closes"][i]
            if s not in entered and o <= level:
                close_pos(s, x, date, i, o * (1 - slip), reason)
            elif low <= level:
                close_pos(s, x, date, i, level * (1 - slip), reason)

        # 3. Entscheidung auf dem Close, Ausführung zur nächsten eigenen Eröffnung
        for s, i in active:
            x = st[s]
            x["last_px"] = x["closes"][i]
            if i < WARMUP:
                continue
            card = strategies.run(strat, x["closes"][: i + 1],
                                  holding=bool(x["qty"]), params=params)
            sig = card["signal"]
            if (sig == "BUY" and not x["qty"]) or (sig == "SELL" and x["qty"]):
                x["pending"] = sig.lower()

        # 4. Trailing-Basis erst NACH allen Checks mit dem heutigen Hoch nachziehen
        for s, i in active:
            x = st[s]
            if x["qty"] and trail_pct:
                h = x["highs"][i] if x["highs"][i] is not None else x["closes"][i]
                x["pos_peak"] = max(x["pos_peak"], h)

        if date >= start:
            eq = mark_equity()
            peak = max(peak, eq)
            max_dd = max(max_dd, 1.0 - eq / peak)
            invested_sum += (eq - cash) / eq
            invested_n += 1
            equity.append({"date": date, "value": round(eq, 6)})

    # Offene Positionen mark-to-market, Rest-Beiträge einsammeln
    for s in syms:
        x = st[s]
        if x["qty"]:
            last = x["closes"][-1]
            x["contrib"] += x["qty"] * (last - x["entry_price"])
            trades.append({
                "symbol": s,
                "entry_date": x["entry_date"], "entry_price": x["entry_price"],
                "exit_date": None, "exit_price": last,
                "pnl_pct": round((last / x["entry_price"] - 1) * 100, 2),
                "bars_held": len(x["dates"]) - 1 - x["entry_bar"],
                "open": True, "exit_reason": None,
            })

    eq_final = mark_equity()
    closed = [t for t in trades if not t["open"]]
    wins = [t for t in closed if t["pnl_pct"] > 0]
    losses = [t for t in closed if t["pnl_pct"] <= 0]
    gross_win = sum(t["pnl_pct"] for t in wins)
    gross_loss = -sum(t["pnl_pct"] for t in losses)

    def pct(x):
        return round(x, 2) if x is not None else None

    # Benchmark: Pott gleich verteilt und liegen lassen — jedes Symbol wird ab
    # seinem eigenen Warmup-Ende gekauft (inkl. Einstiegs-Slippage)
    bh_final = sum(x["closes"][-1] / (x["closes"][WARMUP] * (1 + slip))
                   for x in st.values()) / n

    trades.sort(key=lambda t: (t["entry_date"], t["symbol"]))
    stats = {
        "total_return_pct": pct((eq_final - 1) * 100),
        "buy_hold_return_pct": pct((bh_final - 1) * 100),
        "n_trades": len(trades),
        "n_closed": len(closed),
        "win_rate_pct": pct(len(wins) / len(closed) * 100) if closed else None,
        "avg_win_pct": pct(gross_win / len(wins)) if wins else None,
        "avg_loss_pct": pct(-gross_loss / len(losses)) if losses else None,
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else None,
        "max_drawdown_pct": pct(max_dd * 100),
        "avg_invested_pct": pct(invested_sum / invested_n * 100) if invested_n else None,
        "n_stop_exits": len([t for t in closed
                             if t.get("exit_reason") in ("stop", "trail")]),
    }
    per_symbol = []
    for s in syms:
        ts = [t for t in trades if t["symbol"] == s]
        cs = [t for t in ts if not t["open"]]
        w = [t for t in cs if t["pnl_pct"] > 0]
        per_symbol.append({
            "symbol": s,
            "n_trades": len(ts),
            "win_rate_pct": pct(len(w) / len(cs) * 100) if cs else None,
            # Beitrag zum Gesamtergebnis in Prozentpunkten des Startkapitals
            "contribution_pct": pct(st[s]["contrib"] * 100),
            "open": bool(st[s]["qty"]),
        })
    return {"trades": trades, "stats": stats, "equity": equity,
            "per_symbol": per_symbol}


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


def _parse_common(args):
    """Strategie + Params + Risk + Slippage validieren.
    Liefert (strat, resolved_params, risk, slippage_bps) oder wirft ValueError."""
    strat = strategies.get(args.strategy)
    if strat is None:
        raise ValueError(f"unbekannte Strategie {args.strategy!r}")
    try:
        params = json.loads(args.params) if args.params else {}
        if not isinstance(params, dict):
            raise ValueError("params must be a JSON object")
    except ValueError as e:
        raise ValueError(f"ungültige Params: {e}") from None
    try:
        risk = parse_risk(args.risk)
    except ValueError as e:
        raise ValueError(f"ungültiges Risk-Overlay: {e}") from None
    if not 0 <= args.slippage_bps <= 100:
        raise ValueError("slippage-bps muss zwischen 0 und 100 liegen")
    return strat, strategies.resolve_params(strat, params), risk, args.slippage_bps


def cmd_run(args) -> int:
    try:
        strat, resolved, risk, slippage = _parse_common(args)
    except ValueError as e:
        print(json.dumps({"error": str(e)}, ensure_ascii=False))
        return 1

    symbol = args.symbol.upper()
    dates, opens, highs, lows, closes = load_bars(
        args.db or config.DB_PATH, symbol, args.timeframe)
    if len(closes) <= WARMUP:
        print(json.dumps({"error": f"zu wenig Bars für {symbol} "
                                   f"({len(closes)}, benötigt >{WARMUP})"}))
        return 1

    result = run_backtest(dates, opens, highs, lows, closes, strat, resolved,
                          args.execution, risk, slippage)
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
        "slippage_bps": slippage,
        "macro": None,
    }
    print(json.dumps(result, ensure_ascii=False))
    return 0


def cmd_portfolio(args) -> int:
    try:
        strat, resolved, risk, slippage = _parse_common(args)
    except ValueError as e:
        print(json.dumps({"error": str(e)}, ensure_ascii=False))
        return 1

    db_path = args.db or config.DB_PATH
    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    else:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            symbols = [r[0] for r in con.execute(
                "SELECT symbol FROM watchlist ORDER BY symbol")]
        finally:
            con.close()
    if not symbols:
        print(json.dumps({"error": "keine Symbole (Watchlist leer?)"}))
        return 1

    all_bars, skipped = {}, {}
    for sym in dict.fromkeys(symbols):
        bars = load_bars(db_path, sym, args.timeframe)
        if len(bars[4]) <= WARMUP:
            skipped[sym] = f"zu wenig Bars ({len(bars[4])}, benötigt >{WARMUP})"
        else:
            all_bars[sym] = bars
    if not all_bars:
        print(json.dumps({"error": "kein Symbol hat genug Historie",
                          "skipped": skipped}, ensure_ascii=False))
        return 1

    result = run_portfolio(all_bars, strat, resolved, risk, slippage)
    timeline_from = min(b[0][WARMUP] for b in all_bars.values())
    result["meta"] = {
        "symbols": sorted(all_bars),
        "skipped": skipped or None,
        "strategy": strat.NAME,
        "params": resolved,
        "from": timeline_from,
        "to": max(b[0][-1] for b in all_bars.values()),
        "execution": "next_open",
        "timeframe": args.timeframe,
        "risk": risk or None,
        "slippage_bps": slippage,
        "macro": None,
    }
    print(json.dumps(result, ensure_ascii=False))
    return 0


def _axis_values(spec: dict, max_values: int = 8) -> list[float]:
    """Bis zu `max_values` Werte je Parameter, gleichmäßig über min..max auf
    dem step-Raster verteilt (Default-Grid, wenn kein --grid angegeben ist)."""
    lo, hi = spec["min"], spec["max"]
    step = spec.get("step") or 1
    count = int(round((hi - lo) / step)) + 1
    if count <= max_values:
        vals = [lo + k * step for k in range(count)]
    else:
        vals = sorted({lo + round((hi - lo) * k / (max_values - 1) / step) * step
                       for k in range(max_values)})
    return [round(v, 10) for v in vals]


def build_grid(strat, grid_raw: str | None, base_params: dict) -> list[dict]:
    """Parameter-Kombinationen für den Sweep. --grid = JSON {param: [Werte,…]};
    nicht gelistete Params bleiben auf dem Basis-/Default-Wert. Ohne --grid
    wird das PARAMS-Schema mit bis zu 8 Werten je Achse abgetastet."""
    schema = getattr(strat, "PARAMS", {})
    if grid_raw:
        grid = json.loads(grid_raw)
        if not isinstance(grid, dict) or not grid:
            raise ValueError("grid muss ein JSON-Objekt {param: [Werte,…]} sein")
        unknown = set(grid) - set(schema)
        if unknown:
            raise ValueError(f"grid: unbekannte Params {sorted(unknown)}")
        axes = {}
        for key, vals in grid.items():
            if not isinstance(vals, list):
                vals = [vals]
            if not vals or not all(isinstance(v, (int, float)) for v in vals):
                raise ValueError(f"grid[{key}]: Liste von Zahlen erwartet")
            axes[key] = [float(v) for v in vals]
    else:
        axes = {key: _axis_values(spec) for key, spec in schema.items()}

    base = strategies.resolve_params(strat, base_params)
    if not axes:  # parameterlose Strategie: eine Zelle (reiner Train/Test-Check)
        return [base]

    combos, seen = [], set()

    def add(params: dict) -> None:
        key = json.dumps(params, sort_keys=True)
        if key not in seen:
            seen.add(key)
            combos.append(params)

    add(base)  # Basis-/Default-Kombination immer mit im Rennen
    names = sorted(axes)
    for values in itertools.product(*(axes[k] for k in names)):
        add(strategies.resolve_params(strat, {**base_params, **dict(zip(names, values))}))
    return combos


def cmd_sweep(args) -> int:
    try:
        strat, base, risk, slippage = _parse_common(args)
        if not 0.5 <= args.split <= 0.9:
            raise ValueError("split muss zwischen 0.5 und 0.9 liegen")
        combos = build_grid(strat, args.grid, base)
    except ValueError as e:
        print(json.dumps({"error": str(e)}, ensure_ascii=False))
        return 1
    if len(combos) > MAX_SWEEP_COMBOS:
        print(json.dumps({"error": f"Grid hat {len(combos)} Kombinationen "
                                   f"(Maximum {MAX_SWEEP_COMBOS}) — mit --grid eingrenzen"}))
        return 1

    symbol = args.symbol.upper()
    dates, opens, highs, lows, closes = load_bars(
        args.db or config.DB_PATH, symbol, args.timeframe)
    n = len(closes)
    split_idx = WARMUP + int(round((n - WARMUP) * args.split))
    if split_idx - WARMUP < 40 or n - split_idx < 40:
        print(json.dumps({"error": f"zu wenig Bars für einen {args.split:.0%}-Split "
                                   f"({n} Bars, je Segment mindestens 40 nach Warmup)"},
                         ensure_ascii=False))
        return 1
    split_date = dates[split_idx]
    slip = slippage / 10000.0

    def drawdown(values, start_peak: float) -> float:
        peak, worst = start_peak, 0.0
        for v in values:
            peak = max(peak, v)
            worst = max(worst, 1.0 - v / peak)
        return worst

    def trade_stats(trades: list[dict]) -> tuple[int, float | None]:
        wins = [t for t in trades if t["pnl_pct"] > 0]
        return len(trades), (round(len(wins) / len(trades) * 100, 1) if trades else None)

    results = []
    pos = split_idx - WARMUP  # Equity-Index der Split-Grenze (equity[k] ↔ Bar WARMUP+k)
    for i, params in enumerate(combos, 1):
        r = run_backtest(dates, opens, highs, lows, closes, strat, params,
                         "next_open", risk, slippage)
        equity = [e["value"] for e in r["equity"]]
        eq_split, eq_final = equity[pos - 1], equity[-1]
        closed = [t for t in r["trades"] if not t["open"]]
        tr = [t for t in closed if t["exit_date"] < split_date]
        te = [t for t in closed if t["exit_date"] >= split_date]
        n_tr, win_tr = trade_stats(tr)
        n_te, win_te = trade_stats(te)
        results.append({
            "params": params,
            "train": {"return_pct": round((eq_split - 1) * 100, 2),
                      "max_drawdown_pct": round(drawdown(equity[:pos], 1.0) * 100, 2),
                      "n_trades": n_tr, "win_rate_pct": win_tr},
            "test": {"return_pct": round((eq_final / eq_split - 1) * 100, 2),
                     "max_drawdown_pct": round(drawdown(equity[pos:], eq_split) * 100, 2),
                     "n_trades": n_te, "win_rate_pct": win_te},
        })
        if i % 10 == 0 or i == len(combos):
            print(f"sweep {symbol}: {i}/{len(combos)}", file=sys.stderr)

    # Ranking: In-Sample-Sieger zuerst; test_rank zeigt, wo die Kombination
    # out-of-sample gelandet wäre (großer Abstand = Overfitting-Signal).
    results.sort(key=lambda r: r["train"]["return_pct"], reverse=True)
    for rank, idx in enumerate(sorted(range(len(results)),
                                      key=lambda i: results[i]["test"]["return_pct"],
                                      reverse=True), 1):
        results[idx]["test_rank"] = rank

    out = {
        "results": results,
        "meta": {
            "symbol": symbol,
            "strategy": strat.NAME,
            "timeframe": args.timeframe,
            "execution": "next_open",
            "risk": risk or None,
            "slippage_bps": slippage,
            "split": args.split,
            "split_date": split_date,
            "from": dates[WARMUP],
            "to": dates[-1],
            "n_bars": n,
            "train_bars": split_idx - WARMUP,
            "test_bars": n - split_idx,
            "n_combos": len(combos),
            "buy_hold": {
                "train_pct": round((closes[split_idx - 1]
                                    / (closes[WARMUP] * (1 + slip)) - 1) * 100, 2),
                "test_pct": round((closes[-1]
                                   / (closes[split_idx - 1] * (1 + slip)) - 1) * 100, 2),
            },
        },
    }
    print(json.dumps(out, ensure_ascii=False))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Strategy backtester (JSON output).")
    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="Strategien + Params-Schemata")

    def add_common(p):
        p.add_argument("--strategy", default=strategies.DEFAULT)
        p.add_argument("--params", help="JSON-Objekt mit Param-Overrides")
        p.add_argument("--timeframe", choices=["1d", "1h"], default="1d",
                       help="1h testet auf Stundenkerzen (Tabelle bars_1h)")
        p.add_argument("--risk", help='Risk-Overlay, z. B. {"trail_pct": 3} — '
                                      "Stops werden intra-Kerze gegen Open/Low simuliert")
        p.add_argument("--slippage-bps", type=float, default=5.0,
                       help="Fill-Abschlag in Basispunkten je Seite "
                            "(Default 5 = 0,05 %%; 0 = aus)")
        p.add_argument("--db", help="Pfad zur SQLite-DB (Default: config.DB_PATH)")

    run_p = sub.add_parser("run", help="Backtest für ein Symbol")
    run_p.add_argument("symbol")
    run_p.add_argument("--execution", choices=["next_open", "close"],
                       default="next_open")
    add_common(run_p)

    pf_p = sub.add_parser("portfolio", help="Pott-Modell-Backtest über mehrere "
                                            "Symbole (wie trader.py)")
    pf_p.add_argument("--symbols", help="Kommagetrennt; Default: Watchlist aus der DB")
    add_common(pf_p)

    sw_p = sub.add_parser("sweep", help="Parameter-Sweep mit Train/Test-Split "
                                        "(Optimieren auf Train, blind auswerten auf Test)")
    sw_p.add_argument("symbol")
    sw_p.add_argument("--split", type=float, default=0.7,
                      help="Anteil Trainings-Zeitraum (0.5–0.9, Default 0.7)")
    sw_p.add_argument("--grid", help='JSON {param: [Werte,…]} — Default: bis zu '
                                     "8 Werte je Param aus dem PARAMS-Schema; "
                                     "--params fixiert nicht gelistete Params")
    add_common(sw_p)

    args = ap.parse_args()
    if args.command == "list":
        print(json.dumps(strategies.list_all(), ensure_ascii=False))
        return 0
    if args.command == "sweep":
        return cmd_sweep(args)
    return cmd_portfolio(args) if args.command == "portfolio" else cmd_run(args)


if __name__ == "__main__":
    raise SystemExit(main())
