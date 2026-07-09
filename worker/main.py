#!/usr/bin/env python3
"""Worker entry point.

Commands:
  python main.py init             create DB schema + seed watchlist (idempotent)
  python main.py run [--only A,B] one full daily run (macro + all active symbols)
  python main.py loop             daemon: daily run after US close, forever
  python main.py add SYMBOL       validate via Yahoo search and add to watchlist
  python main.py list             show watchlist with latest action
"""
from __future__ import annotations
import argparse
import datetime as dt
import logging
import sys
import time
from zoneinfo import ZoneInfo

import config
import db
import fetch
import macro_pillar
import score

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


def run_once(con, only: list[str] | None = None) -> None:
    as_of = now_iso()
    log.info("=== daily run %s ===", as_of)

    try:
        macro_score = run_macro(con, as_of)

        rows = db.active_symbols(con)
        if only:
            wanted = {s.upper() for s in only}
            rows = [r for r in rows if r["symbol"].upper() in wanted]
        symbols = [r["symbol"] for r in rows]
        log.info("analyzing %d symbols", len(symbols))

        update_bars(con, symbols)

        for row in rows:
            sym = row["symbol"]
            closes = db.closes(con, sym)
            if len(closes) < 60:
                log.warning("%s: only %d bars — skipping analysis", sym, len(closes))
                continue
            card = score.score_symbol(
                closes,
                macro_score=macro_score,
                symbol=sym,
                holding=bool(row["holding"]),
            )
            db.write_analysis(con, sym, as_of, card)
            log.info("%s: %-28s total=%+d", sym, card["decision"]["action"], card["pillar_total"])

        db.set_meta(con, "last_run_ok", as_of)
        log.info("=== run complete ===")
    except fetch.RateLimited as e:
        # Hard backoff: abort the whole run, keep last good data, try again next cycle.
        db.set_meta(con, "last_rate_limit", now_iso())
        log.error("rate limited by Yahoo — aborting run, last good data stays visible: %s", e)


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


def loop(con) -> None:
    log.info("loop mode: daily run at %s US/Eastern", config.DAILY_RUN_ET)
    while True:
        target = next_run_time()
        wait = (target - dt.datetime.now(target.tzinfo)).total_seconds()
        log.info("next run at %s (in %.1f h)", target.isoformat(), wait / 3600)
        time.sleep(max(wait, 1))
        run_once(con)


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
    args = ap.parse_args()

    con = db.connect(config.DB_PATH)
    if args.command == "init":
        cmd_init(con)
    elif args.command == "run":
        cmd_init(con)  # idempotent — makes sure schema/seed exist
        run_once(con, only=args.only.split(",") if args.only else None)
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
