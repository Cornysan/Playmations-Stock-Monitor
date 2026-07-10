"""SQLite access for the worker. Single writer (worker), multiple readers (web)."""
from __future__ import annotations
import json
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS watchlist (
  symbol      TEXT PRIMARY KEY,
  name        TEXT,
  enabled     INTEGER NOT NULL DEFAULT 1,
  holding     INTEGER NOT NULL DEFAULT 0,   -- 0 = flat (entry framing), 1 = in depot
  added_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bars (
  symbol TEXT NOT NULL,
  date   TEXT NOT NULL,                     -- ISO YYYY-MM-DD
  open   REAL, high REAL, low REAL, close REAL, volume REAL,
  PRIMARY KEY (symbol, date)
);

-- Stundenkerzen (nur reguläre US-Handelszeiten, letzte Tages-Kerze 30 min).
CREATE TABLE IF NOT EXISTS bars_1h (
  symbol TEXT NOT NULL,
  ts     TEXT NOT NULL,                     -- ISO UTC YYYY-MM-DDTHH:MM:SS (Bar-Beginn)
  open   REAL, high REAL, low REAL, close REAL, volume REAL,
  PRIMARY KEY (symbol, ts)
);

CREATE TABLE IF NOT EXISTS analysis (
  symbol          TEXT NOT NULL,
  as_of           TEXT NOT NULL,            -- timestamp of the run
  trend_score     INTEGER,
  momentum_score  INTEGER,
  macro_score     INTEGER,
  pillar_total    INTEGER,
  action          TEXT,
  rationale       TEXT,
  framing         TEXT,
  flags_json      TEXT,
  indicators_json TEXT,
  PRIMARY KEY (symbol, as_of)
);

CREATE TABLE IF NOT EXISTS macro_snapshot (
  as_of           TEXT PRIMARY KEY,
  composite       REAL,
  regime          TEXT,
  pillar_score    INTEGER,
  pillar_label    TEXT,
  components_json TEXT,
  notes_json      TEXT
);

CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT
);

-- Strategie-Auswahl + Parameter-Overrides, getrennt pro Timeframe (1d/1h) —
-- EMA-Perioden bedeuten auf Stundenkerzen etwas anderes als auf Tageskerzen.
-- Neben watchlist die einzige Tabelle, die auch das Web beschreibt.
CREATE TABLE IF NOT EXISTS strategy_config (
  name        TEXT NOT NULL,
  timeframe   TEXT NOT NULL DEFAULT '1d',
  params_json TEXT,
  active      INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (name, timeframe)
);

-- Auto-Trading: jede an Alpaca geschickte Order (auch fehlgeschlagene).
-- client_order_id = "spm-{SYMBOL}-{YYYYMMDD}-{side}" → idempotent pro Tag.
CREATE TABLE IF NOT EXISTS orders (
  client_order_id  TEXT PRIMARY KEY,
  alpaca_id        TEXT,
  symbol           TEXT NOT NULL,
  side             TEXT NOT NULL,            -- buy | sell
  notional         REAL,                     -- Kauf: Dollar-Betrag; Verkauf: NULL (ganze Position)
  submitted_at     TEXT NOT NULL,
  status           TEXT NOT NULL,            -- accepted/new/filled/canceled/… oder error
  filled_qty       REAL,
  filled_avg_price REAL,
  filled_at        TEXT,
  error            TEXT
);

-- Konto-Zustand pro Run (Equity-Verlauf, Positions-Snapshot für die UI).
CREATE TABLE IF NOT EXISTS broker_snapshot (
  as_of          TEXT PRIMARY KEY,
  equity         REAL,
  cash           REAL,
  buying_power   REAL,
  positions_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_analysis_symbol_asof ON analysis(symbol, as_of DESC);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.executescript(SCHEMA)
    _migrate(con)
    return con


def _migrate(con: sqlite3.Connection) -> None:
    cols = {r["name"] for r in con.execute("PRAGMA table_info(analysis)")}
    if "strategy" not in cols:
        con.execute("ALTER TABLE analysis ADD COLUMN strategy TEXT")
    if "signal" not in cols:
        con.execute("ALTER TABLE analysis ADD COLUMN signal TEXT")
    if "timeframe" not in cols:
        con.execute("ALTER TABLE analysis ADD COLUMN timeframe TEXT NOT NULL DEFAULT '1d'")
    wcols = {r["name"] for r in con.execute("PRAGMA table_info(watchlist)")}
    if "autotrade" not in wcols:
        con.execute("ALTER TABLE watchlist ADD COLUMN autotrade INTEGER NOT NULL DEFAULT 0")
    # Auto-Trade-Lock: beim Aktivieren eingefrorene Strategie (+Params, +Timeframe).
    # NULL = kein Lock → globale aktive Strategie / TRADING_TIMEFRAME-Fallback.
    if "strat_name" not in wcols:
        con.execute("ALTER TABLE watchlist ADD COLUMN strat_name TEXT")
        con.execute("ALTER TABLE watchlist ADD COLUMN strat_params TEXT")
        con.execute("ALTER TABLE watchlist ADD COLUMN strat_timeframe TEXT")
    # strategy_config: alte Ein-Spalten-PK-Tabelle (nur name) auf (name, timeframe)
    # umbauen; Bestandszeilen waren faktisch Tages-Konfiguration.
    scols = {r["name"] for r in con.execute("PRAGMA table_info(strategy_config)")}
    if scols and "timeframe" not in scols:
        con.execute("ALTER TABLE strategy_config RENAME TO strategy_config_old")
        con.execute(
            "CREATE TABLE strategy_config ("
            "name TEXT NOT NULL, timeframe TEXT NOT NULL DEFAULT '1d', "
            "params_json TEXT, active INTEGER NOT NULL DEFAULT 0, "
            "PRIMARY KEY (name, timeframe))")
        con.execute(
            "INSERT INTO strategy_config(name, timeframe, params_json, active) "
            "SELECT name, '1d', params_json, active FROM strategy_config_old")
        con.execute("DROP TABLE strategy_config_old")
    con.commit()


# --- watchlist -------------------------------------------------------------

def active_symbols(con: sqlite3.Connection) -> list[sqlite3.Row]:
    return con.execute(
        "SELECT symbol, name, holding, autotrade, strat_name, strat_params, "
        "strat_timeframe FROM watchlist WHERE enabled=1 ORDER BY symbol"
    ).fetchall()


def add_symbol(con: sqlite3.Connection, symbol: str, name: str | None, added_at: str) -> None:
    con.execute(
        "INSERT INTO watchlist(symbol, name, added_at) VALUES(?,?,?) "
        "ON CONFLICT(symbol) DO UPDATE SET enabled=1, name=COALESCE(excluded.name, name)",
        (symbol, name, added_at),
    )
    con.commit()


# --- bars ------------------------------------------------------------------

def last_bar_date(con: sqlite3.Connection, symbol: str) -> str | None:
    row = con.execute("SELECT MAX(date) d FROM bars WHERE symbol=?", (symbol,)).fetchone()
    return row["d"]


def upsert_bars(con: sqlite3.Connection, symbol: str, rows: list[tuple]) -> int:
    """rows: list of (date, open, high, low, close, volume)"""
    con.executemany(
        "INSERT OR REPLACE INTO bars(symbol, date, open, high, low, close, volume) "
        "VALUES(?,?,?,?,?,?,?)",
        [(symbol, *r) for r in rows],
    )
    con.commit()
    return len(rows)


def closes(con: sqlite3.Connection, symbol: str, limit: int = 320) -> list[float]:
    """Close prices old→new (what indicators.compute / score_symbol expect)."""
    rows = con.execute(
        "SELECT close FROM bars WHERE symbol=? AND close IS NOT NULL "
        "ORDER BY date DESC LIMIT ?",
        (symbol, limit),
    ).fetchall()
    return [r["close"] for r in reversed(rows)]


# --- bars_1h -----------------------------------------------------------------

def last_bar_ts_1h(con: sqlite3.Connection, symbol: str) -> str | None:
    row = con.execute("SELECT MAX(ts) t FROM bars_1h WHERE symbol=?", (symbol,)).fetchone()
    return row["t"]


def upsert_bars_1h(con: sqlite3.Connection, symbol: str, rows: list[tuple]) -> int:
    """rows: list of (ts, open, high, low, close, volume), ts = ISO UTC."""
    con.executemany(
        "INSERT OR REPLACE INTO bars_1h(symbol, ts, open, high, low, close, volume) "
        "VALUES(?,?,?,?,?,?,?)",
        [(symbol, *r) for r in rows],
    )
    con.commit()
    return len(rows)


def closes_1h(con: sqlite3.Connection, symbol: str, limit: int = 320) -> list[float]:
    rows = con.execute(
        "SELECT close FROM bars_1h WHERE symbol=? AND close IS NOT NULL "
        "ORDER BY ts DESC LIMIT ?",
        (symbol, limit),
    ).fetchall()
    return [r["close"] for r in reversed(rows)]


# --- analysis / macro ------------------------------------------------------

def write_analysis(con: sqlite3.Connection, symbol: str, as_of: str,
                   card: dict, strategy: str, timeframe: str = "1d") -> None:
    """card: flaches Strategie-Ergebnis (Kontrakt siehe strategies/__init__.py)."""
    con.execute(
        "INSERT OR REPLACE INTO analysis(symbol, as_of, trend_score, momentum_score, "
        "macro_score, pillar_total, action, rationale, framing, flags_json, "
        "indicators_json, strategy, signal, timeframe) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            symbol, as_of,
            card["trend_score"], card["momentum_score"], card["macro_score"],
            card["pillar_total"],
            card["action"], card["rationale"], card["framing"],
            json.dumps(card["flags"]),
            json.dumps(card["indicators"]),
            strategy, card["signal"], timeframe,
        ),
    )
    con.commit()


def active_strategy(con: sqlite3.Connection,
                    timeframe: str = "1d") -> tuple[str, dict] | None:
    """(name, params) der für den Timeframe aktiven Strategie, oder None (→ Default)."""
    row = con.execute(
        "SELECT name, params_json FROM strategy_config WHERE active=1 AND timeframe=? "
        "LIMIT 1", (timeframe,)
    ).fetchone()
    if row is None:
        return None
    try:
        params = json.loads(row["params_json"]) if row["params_json"] else {}
    except ValueError:
        params = {}
    return row["name"], params if isinstance(params, dict) else {}


def write_macro(con: sqlite3.Connection, as_of: str, result) -> None:
    con.execute(
        "INSERT OR REPLACE INTO macro_snapshot(as_of, composite, regime, pillar_score, "
        "pillar_label, components_json, notes_json) VALUES(?,?,?,?,?,?,?)",
        (
            as_of, result.composite, result.regime, result.pillar_score,
            result.pillar_label, json.dumps(result.components), json.dumps(result.notes),
        ),
    )
    con.commit()


# --- auto-trading ------------------------------------------------------------

def autotrade_symbols(con: sqlite3.Connection, timeframe: str | None = None,
                      fallback_tf: str = "1d") -> list[str]:
    """Auto-Trade-Symbole; mit `timeframe` nur die, deren effektiver Timeframe
    (Lock, sonst `fallback_tf`) passt."""
    if timeframe is None:
        return [r["symbol"] for r in con.execute(
            "SELECT symbol FROM watchlist WHERE enabled=1 AND autotrade=1 "
            "ORDER BY symbol")]
    return [r["symbol"] for r in con.execute(
        "SELECT symbol FROM watchlist WHERE enabled=1 AND autotrade=1 "
        "AND COALESCE(strat_timeframe, ?) = ? ORDER BY symbol",
        (fallback_tf, timeframe))]


def upsert_order(con: sqlite3.Connection, o: dict) -> None:
    con.execute(
        "INSERT INTO orders(client_order_id, alpaca_id, symbol, side, notional, "
        "submitted_at, status, filled_qty, filled_avg_price, filled_at, error) "
        "VALUES(:client_order_id, :alpaca_id, :symbol, :side, :notional, "
        ":submitted_at, :status, :filled_qty, :filled_avg_price, :filled_at, :error) "
        "ON CONFLICT(client_order_id) DO UPDATE SET "
        "alpaca_id=excluded.alpaca_id, status=excluded.status, "
        "filled_qty=excluded.filled_qty, filled_avg_price=excluded.filled_avg_price, "
        "filled_at=excluded.filled_at, error=excluded.error",
        {"alpaca_id": None, "notional": None, "filled_qty": None,
         "filled_avg_price": None, "filled_at": None, "error": None, **o},
    )
    con.commit()


def order_exists(con: sqlite3.Connection, client_order_id: str) -> bool:
    return con.execute("SELECT 1 FROM orders WHERE client_order_id=?",
                       (client_order_id,)).fetchone() is not None


def open_orders(con: sqlite3.Connection) -> list[sqlite3.Row]:
    """Orders, deren Status noch nicht final ist (werden beim sync refresht)."""
    return con.execute(
        "SELECT * FROM orders WHERE status NOT IN "
        "('filled','canceled','expired','rejected','error')").fetchall()


def write_broker_snapshot(con: sqlite3.Connection, as_of: str, equity: float,
                          cash: float, buying_power: float, positions: dict) -> None:
    con.execute(
        "INSERT OR REPLACE INTO broker_snapshot(as_of, equity, cash, buying_power, "
        "positions_json) VALUES(?,?,?,?,?)",
        (as_of, equity, cash, buying_power, json.dumps(positions)),
    )
    con.commit()


def set_meta(con: sqlite3.Connection, key: str, value: str) -> None:
    con.execute(
        "INSERT INTO meta(key, value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    con.commit()


def get_meta(con: sqlite3.Connection, key: str) -> str | None:
    row = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None
