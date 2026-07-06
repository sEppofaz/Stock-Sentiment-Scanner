"""SQLite-Layer für Frühsignale. Alle Zugriffe auf signals.db laufen hier durch."""
import json
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "signals.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker        TEXT NOT NULL,
    signal_type   TEXT NOT NULL,      -- 'insider_buy' | 'volume_anomaly' | 'buzz_accel'
    signal_ts     TEXT NOT NULL,      -- ISO 8601, UTC
    score         REAL,
    details_json  TEXT,
    UNIQUE(ticker, signal_type, signal_ts)
);
CREATE INDEX IF NOT EXISTS idx_signals_ticker_ts ON signals(ticker, signal_ts);

CREATE TABLE IF NOT EXISTS edgar_seen (
    accession_no  TEXT PRIMARY KEY,
    seen_ts       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS buzz_history (
    ticker        TEXT NOT NULL,
    date          TEXT NOT NULL,      -- YYYY-MM-DD (Datum des Artikels, nicht des Scans)
    news_count    INTEGER,
    bullish_pct   REAL,
    PRIMARY KEY (ticker, date)
);

CREATE TABLE IF NOT EXISTS alerts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker        TEXT NOT NULL,
    alert_ts      TEXT NOT NULL,
    total_score   REAL,
    signal_ids    TEXT,               -- JSON-Array der beteiligten signals.id
    price_at_alert REAL
);

CREATE TABLE IF NOT EXISTS forward_returns (
    alert_id      INTEGER NOT NULL REFERENCES alerts(id),
    horizon_days  INTEGER NOT NULL,   -- 1 | 5 | 20 (Handelstage)
    ret_pct       REAL,
    filled_ts     TEXT,
    PRIMARY KEY (alert_id, horizon_days)
);
"""


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_conn() as conn:
        conn.executescript(_SCHEMA)


def insert_signal(ticker: str, signal_type: str, signal_ts: str,
                  score: float, details: dict) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO signals (ticker, signal_type, signal_ts, score, details_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (ticker, signal_type, signal_ts, score, json.dumps(details)),
        )


def upsert_buzz_rows(rows: list[tuple]) -> None:
    """rows: [(ticker, date, news_count, bullish_pct), ...]"""
    with get_conn() as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO buzz_history (ticker, date, news_count, bullish_pct) "
            "VALUES (?, ?, ?, ?)",
            rows,
        )
