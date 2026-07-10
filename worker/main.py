#!/usr/bin/env python3
"""Worker entry point.

Commands:
  python main.py init             create DB schema + seed watchlist (idempotent)
  python main.py run [--only A,B] [--timeframe 1h]
                                  one run: 1d = macro + daily bars/signals,
                                  1h = hourly bars/signals only
  python main.py loop             daemon: daily run after US close + hourly runs
                                  during US market hours, forever
  python main.py add SYMBOL       validate via Yahoo search and add to watchlist
  python main.py list             show watchlist with latest action
"""
from __future__ import annotations
import argparse
import datetime as dt
import json
import logging
import sys
import time
from zoneinfo import ZoneInfo

import config
import db
import fetch
import macro_pillar
import strategies
import trader

log = logging.getLogger("worker")


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Bar updates (incremental)
# ---------------------------------------------------------------------------

def update_bars(con, symbols: list[str]) -> None:
    """Fetch missing daily bars for `symbols`, batched by how much history they need."""
    today = dt.date.today()
    full_lookback = today - dt.timedelta(days=config.LOOKBACK_DAYS)

    fresh: list[str] = []          # nothing cached yet → full lookback
    incremental: list[str] = []    # cached → only the recent tail
    inc_start = today
    for sym in symbols:
        last = db.last_bar_date(con, sym)
        if last is None:
            fresh.append(sym)
        else:
            start = dt.date.fromisoformat(last) - dt.timedelta(days=config.INCREMENTAL_OVERLAP_DAYS)
            incremental.append(sym)
            inc_start = min(inc_start, start)

    for group, start in ((fresh, full_lookback), (incremental, inc_start)):
        if not group:
            continue
        data = fetch.download_bars(group, start)
        for sym in group:
            rows = data.get(sym)
            if rows:
                n = db.upsert_bars(con, sym, rows)
                log.info("%s: %d bars upserted", sym, n)
            else:
                log.warning("%s: no data returned (keeping last good state)", sym)


def update_bars_1h(con, symbols: list[str]) -> None:
    """Fetch missing hourly bars, same fresh/incremental split as update_bars."""
    now = dt.datetime.now(dt.timezone.utc)
    full_lookback = now - dt.timedelta(days=config.LOOKBACK_1H_DAYS)

    fresh: list[str] = []
    incremental: list[str] = []
    inc_start = now
    for sym in symbols:
        last = db.last_bar_ts_1h(con, sym)
        if last is None:
            fresh.append(sym)
        else:
            start = (dt.datetime.fromisoformat(last).replace(tzinfo=dt.timezone.utc)
                     - dt.timedelta(hours=config.INCREMENTAL_OVERLAP_HOURS))
            incremental.append(sym)
            inc_start = min(inc_start, start)

    for group, start in ((fresh, full_lookback), (incremental, inc_start)):
        if not group:
            continue
        data = fetch.download_bars_1h(group, start)
        for sym in group:
            rows = data.get(sym)
            if rows:
                n = db.upsert_bars_1h(con, sym, rows)
                log.info("%s: %d 1h-bars upserted", sym, n)
            else:
                log.warning("%s: no 1h data returned (keeping last good state)", sym)


# ---------------------------------------------------------------------------
# Daily run
# ---------------------------------------------------------------------------

def run_macro(con, as_of: str) -> int | None:
    """Update macro ETF bars, score the macro pillar, persist the snapshot."""
    update_bars(con, config.MACRO_ETFS)
    series = {}
    for sym in config.MACRO_ETFS:
        closes = db.closes(con, sym)
        if closes:
            series[sym] = closes
    spread = fetch.fred_yield_spread(config.FRED_API_KEY)
    try:
        result = macro_pillar.score_macro({
            "as_of": as_of,
            "series": series,
            "yield_spread": spread,
        })
    except ValueError as e:
        log.error("macro scoring failed: %s — symbols will be scored without macro", e)
        return None
    db.write_macro(con, as_of, result)
    log.info("macro: composite=%+.3f regime=%s pillar=%+d",
             result.composite, result.regime, result.pillar_score)
    return result.pillar_score


def active_strategy(con, timeframe: str = "1d"):
    """Für den Timeframe aktive Strategie + Params; Fallback auf den Default."""
    name, params = db.active_strategy(con, timeframe) or (strategies.DEFAULT, {})
    strat = strategies.get(name)
    if strat is None:
        log.error("active strategy %r not found — falling back to %s",
                  name, strategies.DEFAULT)
        strat, params = strategies.get(strategies.DEFAULT), {}
    return strat, params


def _locked_strategy(row, timeframe: str):
    """Eingelockte Strategie eines Auto-Trade-Symbols, wenn sie für diesen
    Timeframe gilt — sonst None (→ globale aktive Strategie)."""
    try:
        if not row["autotrade"] or not row["strat_name"]:
            return None
        locked_tf = row["strat_timeframe"] or config.TRADING_TIMEFRAME
    except (KeyError, IndexError):
        return None
    if locked_tf != timeframe:
        return None
    strat = strategies.get(row["strat_name"])
    if strat is None:
        log.error("%s: locked strategy %r not found — using active strategy",
                  row["symbol"], row["strat_name"])
        return None
    try:
        params = json.loads(row["strat_params"]) if row["strat_params"] else {}
    except ValueError:
        params = {}
    return strat, (params if isinstance(params, dict) else {})


def analyze_symbols(con, rows, macro_score: int | None, as_of: str,
                    timeframe: str = "1d") -> None:
    symbols = [r["symbol"] for r in rows]
    if timeframe == "1h":
        update_bars_1h(con, symbols)
    else:
        update_bars(con, symbols)
    default_strat, default_params = active_strategy(con, timeframe)
    log.info("strategy[%s]: %s params=%s", timeframe, default_strat.NAME,
             strategies.resolve_params(default_strat, default_params))
    for row in rows:
        sym = row["symbol"]
        closes = db.closes_1h(con, sym) if timeframe == "1h" else db.closes(con, sym)
        if len(closes) < 60:
            log.warning("%s: only %d bars — skipping analysis", sym, len(closes))
            continue
        # Auto-Trade-Symbole mit Lock werden mit IHRER Strategie analysiert —
        # das Signal, das der Trader liest, kommt genau aus der Konfiguration,
        # die beim Aktivieren eingefroren wurde.
        strat, params = _locked_strategy(row, timeframe) or (default_strat, default_params)
        card = strategies.run(
            strat, closes,
            holding=bool(row["holding"]),
            params=params,
            macro_score=macro_score,
        )
        db.write_analysis(con, sym, as_of, card, strat.NAME, timeframe)
        log.info("%s: %-28s signal=%s%s", sym, card["action"], card["signal"],
                 " [locked]" if strat is not default_strat else "")


def run_once(con, only: list[str] | None = None) -> None:
    as_of = now_iso()
    log.info("=== daily run %s ===", as_of)

    # Broker-Fehler dürfen die Analyse nie verhindern (und umgekehrt bricht
    # ein Yahoo-Rate-Limit unten den Run ab, BEVOR gehandelt wird — es wird
    # nie auf Basis veralteter Signale geordert).
    trading = trader.enabled()
    db.set_meta(con, "trading_enabled", "1" if trading else "0")
    db.set_meta(con, "trading_timeframe", config.TRADING_TIMEFRAME)
    if trading:
        try:
            trader.sync(con)
        except Exception as e:
            log.error("trader sync failed (run continues without trading): %s", e)
            trading = False

    try:
        macro_score = run_macro(con, as_of)

        rows = db.active_symbols(con)
        if only:
            wanted = {s.upper() for s in only}
            rows = [r for r in rows if r["symbol"].upper() in wanted]
        log.info("analyzing %d symbols", len(rows))
        analyze_symbols(con, rows, macro_score, as_of)

        # trade() handelt nur Symbole, deren effektiver Timeframe (Lock,
        # sonst TRADING_TIMEFRAME) zum Run passt.
        if trading:
            try:
                trader.trade(con, as_of, timeframe="1d")
            except Exception as e:
                log.error("trading failed (analysis is unaffected): %s", e)

        db.set_meta(con, "last_run_ok", as_of)
        log.info("=== run complete ===")
    except fetch.RateLimited as e:
        # Hard backoff: abort the whole run, keep last good data, try again next cycle.
        db.set_meta(con, "last_rate_limit", now_iso())
        log.error("rate limited by Yahoo — aborting run, last good data stays visible: %s", e)


def run_hourly(con, only: list[str] | None = None) -> None:
    """1h-Lauf während der US-Handelszeiten: Stundenkerzen + 1h-Signale.

    Kein Macro-Refresh (der ist Tages-Sache) — die 1h-Analyse bekommt den
    Score des letzten Macro-Snapshots injiziert.
    """
    as_of = now_iso()
    log.info("=== hourly run %s ===", as_of)

    trading = trader.enabled()
    if trading:
        try:
            trader.sync(con)
        except Exception as e:
            log.error("trader sync failed (run continues without trading): %s", e)
            trading = False

    try:
        macro_row = con.execute(
            "SELECT pillar_score FROM macro_snapshot ORDER BY as_of DESC LIMIT 1"
        ).fetchone()
        macro_score = macro_row["pillar_score"] if macro_row else None

        rows = db.active_symbols(con)
        if only:
            wanted = {s.upper() for s in only}
            rows = [r for r in rows if r["symbol"].upper() in wanted]
        log.info("analyzing %d symbols (1h)", len(rows))
        analyze_symbols(con, rows, macro_score, as_of, timeframe="1h")

        if trading:
            try:
                trader.trade(con, as_of, timeframe="1h")
            except Exception as e:
                log.error("trading failed (analysis is unaffected): %s", e)

        db.set_meta(con, "last_hourly_ok", as_of)
        log.info("=== hourly run complete ===")
    except fetch.RateLimited as e:
        db.set_meta(con, "last_rate_limit", now_iso())
        log.error("rate limited by Yahoo — aborting hourly run: %s", e)


def catch_up(con) -> None:
    """Analyze symbols added via the UI since the last run (no full re-run)."""
    # Only recently added symbols: dead tickers from the seed would otherwise be
    # retried every 15 minutes forever (the daily run still retries them once a day).
    cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=48)).isoformat(timespec="seconds")
    rows = con.execute(
        """SELECT symbol, name, holding, autotrade, strat_name, strat_params,
                  strat_timeframe FROM watchlist w
           WHERE enabled = 1 AND added_at >= ? AND NOT EXISTS
             (SELECT 1 FROM analysis a WHERE a.symbol = w.symbol)""",
        (cutoff,),
    ).fetchall()
    if not rows:
        return
    macro_row = con.execute(
        "SELECT pillar_score FROM macro_snapshot ORDER BY as_of DESC LIMIT 1"
    ).fetchone()
    macro_score = macro_row["pillar_score"] if macro_row else None
    log.info("catch-up: %d new symbol(s): %s",
             len(rows), ", ".join(r["symbol"] for r in rows))
    try:
        analyze_symbols(con, rows, macro_score, now_iso())
    except fetch.RateLimited as e:
        log.error("rate limited during catch-up — will retry next cycle: %s", e)


# ---------------------------------------------------------------------------
# Loop mode (daemon)
# ---------------------------------------------------------------------------

def next_run_time() -> dt.datetime:
    et = ZoneInfo("America/New_York")
    hh, mm = (int(x) for x in config.DAILY_RUN_ET.split(":"))
    now = dt.datetime.now(et)
    target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if target <= now:
        target += dt.timedelta(days=1)
    return target


# Stunden-Slots Mo–Fr, ET: jeweils 35 min nach voller Stunde, damit die zuletzt
# abgeschlossene Kerze (Bar-Beginn xx:30) sicher vollständig bei Yahoo liegt.
# 10:35 = erste fertige Kerze (09:30–10:30) … 16:35 = Schlusskerze (15:30–16:00).
HOURLY_SLOTS_ET = [(h, 35) for h in range(10, 17)]


def next_hourly_time() -> dt.datetime:
    et = ZoneInfo("America/New_York")
    now = dt.datetime.now(et)
    for add_days in range(0, 8):
        day = now + dt.timedelta(days=add_days)
        if day.weekday() >= 5:  # Sa/So: Markt zu, keine neuen Kerzen
            continue
        for hh, mm in HOURLY_SLOTS_ET:
            target = day.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if target > now:
                return target
    raise RuntimeError("no hourly slot found")  # unreachable


def loop(con) -> None:
    log.info("loop mode: daily run at %s ET, hourly runs %02d:%02d–%02d:%02d ET "
             "(Mon–Fri), catch-up check every 15 min",
             config.DAILY_RUN_ET, *HOURLY_SLOTS_ET[0], *HOURLY_SLOTS_ET[-1])
    while True:
        daily, hourly = next_run_time(), next_hourly_time()
        target, kind = min((daily, "daily"), (hourly, "hourly"))
        log.info("next %s run at %s", kind, target.isoformat())
        while True:
            remaining = (target - dt.datetime.now(target.tzinfo)).total_seconds()
            if remaining <= 0:
                break
            time.sleep(min(remaining, 900))
            catch_up(con)  # picks up symbols added via the UI in between
        if kind == "daily":
            run_once(con)
        else:
            run_hourly(con)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def cmd_init(con) -> None:
    from seed_tickers import TICKERS
    added = 0
    for sym, name in TICKERS.items():
        if con.execute("SELECT 1 FROM watchlist WHERE symbol=?", (sym,)).fetchone() is None:
            db.add_symbol(con, sym, name, now_iso())
            added += 1
    log.info("schema ready, watchlist seeded (%d new, %d total)",
             added, con.execute("SELECT COUNT(*) c FROM watchlist").fetchone()["c"])


def cmd_add(con, query: str) -> None:
    matches = fetch.search_symbols(query)
    if not matches:
        log.error("no Yahoo match for %r", query)
        sys.exit(1)
    exact = next((m for m in matches if m["symbol"].upper() == query.upper()), matches[0])
    db.add_symbol(con, exact["symbol"], exact["name"], now_iso())
    log.info("added %s (%s)", exact["symbol"], exact["name"])


def cmd_list(con) -> None:
    rows = con.execute(
        """SELECT w.symbol, w.name, w.holding, a.action, a.pillar_total
           FROM watchlist w
           LEFT JOIN analysis a ON a.symbol = w.symbol
             AND a.as_of = (SELECT MAX(as_of) FROM analysis WHERE symbol = w.symbol)
           WHERE w.enabled = 1 ORDER BY w.symbol"""
    ).fetchall()
    for r in rows:
        total = f"{r['pillar_total']:+d}" if r["pillar_total"] is not None else "  ?"
        print(f"{r['symbol']:<10} {total:>3}  {(r['action'] or '—'):<30} {r['name'] or ''}"
              f"{'  [holding]' if r['holding'] else ''}")


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    ap = argparse.ArgumentParser(description="stocks.playmations.com worker")
    ap.add_argument("command", choices=["init", "run", "loop", "add", "list"])
    ap.add_argument("arg", nargs="?", help="symbol for `add`")
    ap.add_argument("--only", help="comma-separated symbols (run only these)")
    ap.add_argument("--timeframe", choices=["1d", "1h"], default="1d",
                    help="run: 1d = voller Tageslauf, 1h = Stunden-Lauf")
    args = ap.parse_args()

    con = db.connect(config.DB_PATH)
    if args.command == "init":
        cmd_init(con)
    elif args.command == "run":
        cmd_init(con)  # idempotent — makes sure schema/seed exist
        only = args.only.split(",") if args.only else None
        if args.timeframe == "1h":
            run_hourly(con, only=only)
        else:
            run_once(con, only=only)
    elif args.command == "loop":
        cmd_init(con)
        loop(con)
    elif args.command == "add":
        if not args.arg:
            ap.error("add requires a symbol")
        cmd_add(con, args.arg)
    elif args.command == "list":
        cmd_list(con)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
