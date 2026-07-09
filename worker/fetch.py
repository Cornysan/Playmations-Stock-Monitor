"""Yahoo (yfinance) + FRED fetching with politeness safeguards.

Strategy (see PROJEKTPLAN §4):
  - batch downloads via yf.download (chunked, jittered pauses between chunks)
  - incremental: only re-fetch the last few days once history exists
  - hard exponential backoff on rate limiting: a 429 aborts the run,
    the next scheduled run tries again (graceful degradation — last good
    values stay in the DB and remain visible in the UI)
"""
from __future__ import annotations
import datetime as dt
import logging
import math
import random
import time

import requests
import yfinance as yf

import config

log = logging.getLogger("fetch")


class RateLimited(Exception):
    """Raised when Yahoo rate-limits us; caller should abort the run."""


def _chunks(seq: list, n: int):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _clean(value) -> float | None:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else f


def download_bars(symbols: list[str], start: dt.date) -> dict[str, list[tuple]]:
    """Batch-download daily OHLCV since `start` for all symbols.

    Returns {symbol: [(date, open, high, low, close, volume), ...]} old→new.
    Symbols that returned nothing are simply absent from the dict.
    """
    out: dict[str, list[tuple]] = {}
    chunks = list(_chunks(symbols, config.CHUNK_SIZE))
    for idx, chunk in enumerate(chunks):
        log.info("downloading chunk %d/%d (%d symbols, start=%s)",
                 idx + 1, len(chunks), len(chunk), start)
        try:
            df = yf.download(
                chunk,
                start=start.isoformat(),
                interval="1d",
                auto_adjust=True,
                group_by="ticker",
                threads=False,
                progress=False,
            )
        except Exception as e:  # yfinance wraps rate limits in various ways
            if "429" in str(e) or "rate" in str(e).lower():
                raise RateLimited(str(e)) from e
            log.error("chunk download failed: %s", e)
            continue
        if df is None or df.empty:
            log.warning("chunk returned no data")
            continue

        for sym in chunk:
            try:
                sub = df[sym] if len(chunk) > 1 else df
            except KeyError:
                continue
            sub = sub.dropna(subset=["Close"])
            rows = []
            for ts, row in sub.iterrows():
                rows.append((
                    ts.date().isoformat(),
                    _clean(row.get("Open")), _clean(row.get("High")),
                    _clean(row.get("Low")), _clean(row.get("Close")),
                    _clean(row.get("Volume")),
                ))
            if rows:
                out[sym] = rows

        if idx < len(chunks) - 1:
            pause = random.uniform(*config.CHUNK_PAUSE_RANGE)
            time.sleep(pause)
    return out


def fred_yield_spread(api_key: str, days: int = 150) -> list[float] | None:
    """10Y-2Y spread series (old→new) from FRED T10Y2Y. None if unavailable."""
    if not api_key:
        return None
    start = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    url = (
        "https://api.stlouisfed.org/fred/series/observations"
        f"?series_id=T10Y2Y&api_key={api_key}&file_type=json&observation_start={start}"
    )
    try:
        resp = requests.get(url, timeout=20)
        resp.raise_for_status()
        obs = resp.json().get("observations", [])
        values = [float(o["value"]) for o in obs if o.get("value") not in (None, ".", "")]
        return values or None
    except Exception as e:
        log.warning("FRED fetch failed (%s) — macro will redistribute the curve weight", e)
        return None


def search_symbols(query: str, count: int = 8) -> list[dict]:
    """Yahoo symbol search (used by `main.py add` for validation)."""
    try:
        s = yf.Search(query, max_results=count)
        return [
            {
                "symbol": q.get("symbol"),
                "name": q.get("longname") or q.get("shortname"),
                "exchange": q.get("exchange"),
                "type": q.get("quoteType"),
            }
            for q in (s.quotes or [])
            if q.get("symbol")
        ]
    except Exception as e:
        log.error("symbol search failed: %s", e)
        return []
