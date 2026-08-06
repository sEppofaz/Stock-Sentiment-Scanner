"""SQLite-Layer für Frühsignale. Alle Zugriffe auf signals.db laufen hier durch."""
import json
import sqlite3
from contextlib import contextmanager
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
    price_at_alert REAL,
    kind          TEXT NOT NULL DEFAULT 'instant'  -- 'instant' | 'combo'
);

CREATE TABLE IF NOT EXISTS forward_returns (
    alert_id      INTEGER NOT NULL REFERENCES alerts(id),
    horizon_days  INTEGER NOT NULL,   -- 1 | 5 | 20 (Handelstage)
    ret_pct       REAL,
    filled_ts     TEXT,
    PRIMARY KEY (alert_id, horizon_days)
);

CREATE TABLE IF NOT EXISTS scan_snapshots (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker            TEXT NOT NULL,
    snapshot_ts       TEXT NOT NULL,   -- = results.json scanned_at des Vollscans, ISO 8601 UTC
    rank              INTEGER,         -- Position in Top-N (1 = höchster Score)
    score             REAL,
    bullish_pct       REAL,
    bearish_pct       REAL,
    buzz              REAL,
    articles_week     INTEGER,
    sentiment_norm    REAL,
    market_cap        INTEGER,
    pe                REAL,
    pinned            INTEGER NOT NULL DEFAULT 0,
    price_at_snapshot REAL,            -- NULL bis scan_tracker.py sie nachträgt (yfinance Tages-Close)
    claude_confidence REAL,            -- nur gesetzt wenn ki_enabled zum Scan-Zeitpunkt aktiv war
    avg_volume_10d    REAL,            -- Finnhub 10DayAverageTradingVolume (Mio. Aktien)
    avg_volume_3m     REAL,            -- Finnhub 3MonthAverageTradingVolume (Mio. Aktien)
    week52_high       REAL,
    week52_low        REAL,
    beta              REAL,
    sector            TEXT,            -- Finnhub finnhubIndustry (/stock/profile2)
    float_shares      REAL,            -- Finnhub floatingShare (Mio. Aktien, /stock/profile2)
    UNIQUE(ticker, snapshot_ts)
);
CREATE INDEX IF NOT EXISTS idx_scan_snapshots_ticker_ts ON scan_snapshots(ticker, snapshot_ts);

CREATE TABLE IF NOT EXISTS scan_forward_returns (
    snapshot_id   INTEGER NOT NULL REFERENCES scan_snapshots(id),
    horizon_days  INTEGER NOT NULL,   -- 1 | 5 | 20 (Handelstage), gleiche Horizonte wie forward_returns
    ret_pct       REAL,
    filled_ts     TEXT,
    PRIMARY KEY (snapshot_id, horizon_days)
);

CREATE TABLE IF NOT EXISTS weekly_reports (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    report_ts          TEXT NOT NULL,   -- ISO 8601 UTC, Erzeugungszeitpunkt
    system             TEXT NOT NULL,   -- 'sentiment' | 'early_signals'
    period_start       TEXT NOT NULL,
    period_end         TEXT NOT NULL,
    sample_size        INTEGER NOT NULL,
    insufficient_data  INTEGER NOT NULL DEFAULT 0,
    report_json        TEXT NOT NULL,
    ai_text            TEXT,
    ai_generated_ts    TEXT
);
CREATE INDEX IF NOT EXISTS idx_weekly_reports_system_ts ON weekly_reports(system, report_ts DESC);
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def get_conn():
    """`with get_conn() as conn:` committet (oder rollt zurück) UND schließt die
    Connection – sqlite3.Connection als Context-Manager selbst schließt nicht,
    das lief bisher nur zufällig über CPython-Refcounting (G1)."""
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        conn.executescript(_SCHEMA)
        # Migration: bestehende alerts-Tabelle (vor 2026-07-09) hat noch
        # keine kind-Spalte – CREATE TABLE IF NOT EXISTS legt sie dort nicht
        # nach.
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(alerts)").fetchall()]
        if "kind" not in cols:
            conn.execute("ALTER TABLE alerts ADD COLUMN kind TEXT NOT NULL DEFAULT 'instant'")

        # Migration: scan_snapshots vor 2026-08-06 (erster Rollout dieser Session)
        # hatte noch keine erweiterten Felder (confidence/Volumen/52W/Beta/Sektor/Float)
        snap_cols = [r["name"] for r in conn.execute("PRAGMA table_info(scan_snapshots)").fetchall()]
        for col, coltype in (
            ("claude_confidence", "REAL"), ("avg_volume_10d", "REAL"), ("avg_volume_3m", "REAL"),
            ("week52_high", "REAL"), ("week52_low", "REAL"), ("beta", "REAL"),
            ("sector", "TEXT"), ("float_shares", "REAL"),
        ):
            if col not in snap_cols:
                conn.execute(f"ALTER TABLE scan_snapshots ADD COLUMN {col} {coltype}")


def cleanup_old_data() -> tuple[int, int]:
    """Prunt operative Historientabellen, die unbegrenzt wachsen (M8).
    signals/alerts/forward_returns sowie scan_snapshots/scan_forward_returns/
    weekly_reports bleiben unangetastet – das ist die Validierungshistorie
    (Trefferquote/Rendite, wöchentliche Performance-Analyse), die soll
    erhalten bleiben."""
    with get_conn() as conn:
        buzz_deleted = conn.execute(
            "DELETE FROM buzz_history WHERE date < date('now', '-60 days')"
        ).rowcount
        edgar_deleted = conn.execute(
            "DELETE FROM edgar_seen WHERE seen_ts < strftime('%Y-%m-%dT%H:%M:%S', 'now', '-30 days')"
        ).rowcount
    return buzz_deleted, edgar_deleted


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


def insert_scan_snapshots(snapshot_ts: str, top_n: list[dict]) -> None:
    """Persistiert die Top-N-Ergebnisse eines Vollscans als Snapshot-Historie
    (results.json wird bei jedem Scan überschrieben, hier bleibt die Zeitreihe
    erhalten). Legt pro Snapshot sofort die drei offenen scan_forward_returns-
    Zeilen an (ret_pct NULL) – analog layer4_scoring._create_alert()."""
    with get_conn() as conn:
        for rank, r in enumerate(top_n, start=1):
            cur = conn.execute(
                "INSERT OR IGNORE INTO scan_snapshots "
                "(ticker, snapshot_ts, rank, score, bullish_pct, bearish_pct, buzz, "
                " articles_week, sentiment_norm, market_cap, pe, pinned, "
                " claude_confidence, avg_volume_10d, avg_volume_3m, week52_high, "
                " week52_low, beta, sector, float_shares) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    r["ticker"], snapshot_ts, rank, r.get("score"),
                    r.get("bullish_pct"), r.get("bearish_pct"), r.get("buzz"),
                    r.get("articles_week"), r.get("sentiment_norm"),
                    r.get("market_cap"), r.get("pe"), 1 if r.get("pinned") else 0,
                    r.get("claude_confidence"), r.get("avg_volume_10d"), r.get("avg_volume_3m"),
                    r.get("week52_high"), r.get("week52_low"), r.get("beta"),
                    r.get("sector"), r.get("float_shares"),
                ),
            )
            if cur.rowcount == 0:
                continue
            snapshot_id = cur.lastrowid
            conn.executemany(
                "INSERT OR IGNORE INTO scan_forward_returns (snapshot_id, horizon_days) "
                "VALUES (?, ?)",
                [(snapshot_id, h) for h in (1, 5, 20)],
            )
