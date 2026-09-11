"""Verifiziert bereits gefüllte forward_returns/scan_forward_returns-Zeilen
erneut gegen einen frischen yfinance-Fetch (2026-09-11, Fable-Review-Nachfund).

Hintergrund: forward_tracker.py/scan_tracker.py füllen ret_pct GENAU EINMAL,
wenn der Horizont erreicht ist, und rühren die Zeile danach nie wieder an.
Live gefunden: Yahoo Finance liefert für einzelne Handelstage teils
vorübergehend fehlerhafte Closes, die Tage/Wochen später stillschweigend
korrigiert werden – SXTC zeigte beim ersten Fill +4.747,83%, derselbe
20-Tage-Horizont lieferte bei einem frischen Fetch Wochen später −39,4%
(baseline unverändert, nur der Horizont-Close war anfangs falsch). Betraf
mehrere Ticker gleichzeitig (SXTC/OMH/ONFO/NXTT), war also kein Einzelfall.
Split-Adjustment (ADR-019, auto_adjust=True) war NICHT die Ursache - das ist
ein eigenständiges Datenqualitätsproblem.

Fix: re-fetcht die Close-Reihe für bereits gefüllte Zeilen und überschreibt
ret_pct nur bei einer groben Abweichung (>=30 Prozentpunkte, verhindert
Flip-Flop bei normalem Kursrauschen/Dividenden-Rundungen). Nach (ticker,
ref_ts) gruppiert - ein yfinance-Call pro Ticker+Datum statt einer je Zeile."""
import logging
from datetime import datetime, timezone

from signals_db import get_conn
from yf_helper import fetch_closes

log = logging.getLogger("scanner")

DISCREPANCY_THRESHOLD_PP = 30


def _reverify(table: str, id_col: str, join_table: str, ts_col: str,
              since_days: int | None) -> int:
    query = (
        f"SELECT t.{id_col} AS id, t.horizon_days, t.ret_pct AS old_ret, "
        f"       j.ticker, j.{ts_col} AS ref_ts "
        f"FROM {table} t JOIN {join_table} j ON j.id = t.{id_col} "
        f"WHERE t.ret_pct IS NOT NULL"
    )
    params: list = []
    if since_days is not None:
        query += " AND t.filled_ts >= strftime('%Y-%m-%dT%H:%M:%S', 'now', ?)"
        params.append(f"-{since_days} days")

    with get_conn() as conn:
        rows = [dict(r) for r in conn.execute(query, params).fetchall()]

    by_key: dict[tuple, list] = {}
    for r in rows:
        by_key.setdefault((r["ticker"], r["ref_ts"][:10]), []).append(r)

    updated = 0
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for (ticker, ref_date), group in by_key.items():
        closes = fetch_closes(ticker, ref_date)
        if closes is None:
            continue
        baseline = float(closes.iloc[0])
        if baseline <= 0:
            continue
        for r in group:
            if len(closes) <= r["horizon_days"]:
                continue
            new_ret = round((float(closes.iloc[r["horizon_days"]]) / baseline - 1) * 100, 2)
            if abs(new_ret - r["old_ret"]) < DISCREPANCY_THRESHOLD_PP:
                continue
            with get_conn() as conn:
                conn.execute(
                    f"UPDATE {table} SET ret_pct=?, filled_ts=? WHERE {id_col}=? AND horizon_days=?",
                    (new_ret, now_iso, r["id"], r["horizon_days"]))
            log.info("Reverify %s %s h=%d: %.2f%% -> %.2f%% (Yahoo-Datenkorrektur)",
                      table, ticker, r["horizon_days"], r["old_ret"], new_ret)
            updated += 1
    return updated


def reverify_recent(since_days: int = 14) -> tuple[int, int]:
    """Für den täglichen Cleanup-Job (app.py::_do_cleanup) – prüft nur kürzlich
    gefüllte Zeilen erneut, damit die tägliche Ticker-/API-Last begrenzt bleibt."""
    n_fwd = _reverify("forward_returns", "alert_id", "alerts", "alert_ts", since_days)
    n_scan = _reverify("scan_forward_returns", "snapshot_id", "scan_snapshots", "snapshot_ts", since_days)
    return n_fwd, n_scan


def reverify_all() -> tuple[int, int]:
    """Für das einmalige Backfill-Script – prüft die komplette Historie."""
    n_fwd = _reverify("forward_returns", "alert_id", "alerts", "alert_ts", None)
    n_scan = _reverify("scan_forward_returns", "snapshot_id", "scan_snapshots", "snapshot_ts", None)
    return n_fwd, n_scan
