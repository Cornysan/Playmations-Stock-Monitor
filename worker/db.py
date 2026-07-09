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

CREATE INDEX IF NOT EXISTS idx_analysis_symbol_asof ON analysis(symbol, as_of DESC);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.executescript(SCHEMA)
    return con


# --- watchlist -------------------------------------------------------------

def active_symbols(con: sqlite3.Connection) -> list[sqlite3.Row]:
    return con.execute(
        "SELECT symbol, name, holding FROM watchlist WHERE enabled=1 ORDER BY symbol"
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


# --- analysis / macro ------------------------------------------------------

def write_analysis(con: sqlite3.Connection, symbol: str, as_of: str, card: dict) -> None:
    p = card["pillars"]
    d = card["decision"]
    con.execute(
        "INSERT OR REPLACE INTO analysis(symbol, as_of, trend_score, momentum_score, "
        "macro_score, pillar_total, action, rationale, framing, flags_json, indicators_json) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            symbol, as_of,
            p["trend"]["score"], p["momentum"]["score"], p["macro_sentiment"]["score"],
            card["pillar_total"],
            d["action"], d["rationale"], d["framing"],
            json.dumps({
                **d["flags"],
                "trend_detail": p["trend"]["detail"],
                "momentum_detail": p["momentum"]["detail"],
                "warning": card.get("warning"),
            }),
            json.dumps(card["indicators"]),
        ),
    )
    con.commit()


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
