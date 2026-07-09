"""Central configuration for the worker. Reads optional .env at repo root."""
from __future__ import annotations
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader (stdlib only). Existing env vars win."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


_load_dotenv(ROOT / ".env")

DB_PATH = Path(os.environ.get("STOCKS_DB", str(ROOT / "data" / "stocks.db")))
FRED_API_KEY = os.environ.get("FRED_API_KEY", "")

# ETFs consumed by macro_pillar.score_macro (one fetch per day, shared by all symbols)
MACRO_ETFS = ["SPY", "RSP", "IWM", "HYG", "LQD", "TLT", "XLY", "XLP"]

# ~420 calendar days ≈ 290 trading days → enough for EMA200 + slope warmup
LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", "420"))
# When updating incrementally, re-fetch the last few days to pick up corrections
INCREMENTAL_OVERLAP_DAYS = 7

# Politeness towards Yahoo: download in chunks with jittered pauses
CHUNK_SIZE = 20
CHUNK_PAUSE_RANGE = (3.0, 7.0)

# Daily run time (US market close + settle buffer), interpreted in US/Eastern
DAILY_RUN_ET = os.environ.get("DAILY_RUN_ET", "17:30")
