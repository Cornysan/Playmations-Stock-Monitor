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

# ~5 Jahre + Puffer: Walk-Forward/Sweeps brauchen lange Zeiträume. Die Analyse
# selbst liest weiterhin nur die letzten ~320 Closes (db.closes limit) — mehr
# Historie kostet also nur Backtest-Laufzeit, nicht den Tagesrun.
LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", "1900"))
# When updating incrementally, re-fetch the last few days to pick up corrections
INCREMENTAL_OVERLAP_DAYS = 7

# Politeness towards Yahoo: download in chunks with jittered pauses
CHUNK_SIZE = 20
CHUNK_PAUSE_RANGE = (3.0, 7.0)

# Daily run time (US market close + settle buffer), interpreted in US/Eastern
DAILY_RUN_ET = os.environ.get("DAILY_RUN_ET", "17:30")

# --- 1h-Timeframe -------------------------------------------------------------
# Lookback für Stundenkerzen: ~120 Kalendertage ≈ 82 Handelstage ≈ 570 1h-Bars
# (genug für EMA200-Warmup auf Stundenbasis; Yahoo-Limit für 1h sind 730 Tage).
LOOKBACK_1H_DAYS = int(os.environ.get("LOOKBACK_1H_DAYS", "120"))
INCREMENTAL_OVERLAP_HOURS = 24

# --- Auto-Trading (Alpaca) ---------------------------------------------------
# Ohne TRADING_ENABLED=1 wird NIE gehandelt, egal ob Keys vorhanden sind.
ALPACA_KEY_ID = os.environ.get("ALPACA_KEY_ID", "")
ALPACA_SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY", "")
ALPACA_BASE_URL = os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
TRADING_ENABLED = os.environ.get("TRADING_ENABLED", "0") == "1"
MAX_ORDERS_PER_RUN = int(os.environ.get("MAX_ORDERS_PER_RUN", "20"))
# Fallback-Timeframe für Auto-Trade-Symbole OHNE eingelockte Strategie
# (watchlist.strat_timeframe). Neue Locks kommen aus dem UI; dieses Flag
# betrifft nur Alt-Symbole, die vor dem Lock-Feature aktiviert wurden.
TRADING_TIMEFRAME = os.environ.get("TRADING_TIMEFRAME", "1d")
