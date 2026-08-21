"""Füllt scan_forward_returns wenn der Handelstage-Horizont (1/5/20) erreicht
ist – Pendant zu forward_tracker.py, aber für die Sentiment-Scan-Snapshot-
Historie (scan_snapshots) statt für Frühsignal-Alerts.

Referenzkurs (price_at_snapshot) kommt bewusst NICHT von Finnhub (kein extra
API-Call), sondern wird beim ersten Tracker-Lauf aus derselben yfinance-
Tages-Close-Reihe entnommen wie der Forward-Preis (closes.iloc[0]) – siehe
ADR-009. Methodisch konsistent, 0 zusätzliche Kosten."""
import logging
from datetime import datetime, timezone

from signals_db import get_conn
from yf_helper import fetch_closes

log = logging.getLogger("scanner")


def run_scan_tracker(cfg: dict) -> None:
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with get_conn() as conn:
        open_rows = conn.execute(
            "SELECT sfr.snapshot_id, sfr.horizon_days, s.ticker, s.snapshot_ts, "
            "       s.price_at_snapshot "
            "FROM scan_forward_returns sfr JOIN scan_snapshots s ON s.id = sfr.snapshot_id "
            "WHERE sfr.ret_pct IS NULL").fetchall()

    # Nach (ticker, snapshot_id) gruppieren – ein yfinance-Call pro Snapshot
    # statt bis zu drei identischer Downloads (analog G7-Fix in forward_tracker.py)
    by_snapshot: dict[tuple, list] = {}
    for r in open_rows:
        by_snapshot.setdefault((r["ticker"], r["snapshot_id"]), []).append(r)

    filled = 0
    for (ticker, snapshot_id), rows in by_snapshot.items():
        snapshot_ts = rows[0]["snapshot_ts"]
        price_at_snapshot = rows[0]["price_at_snapshot"]
        closes = fetch_closes(ticker, snapshot_ts[:10])
        if closes is None:
            continue

        # Referenzkurs kommt seit 2026-08-21 IMMER frisch aus closes.iloc[0]
        # dieser (jetzt Split-bereinigten) Abfrage, nie aus einem zuvor
        # gespeicherten price_at_snapshot - der könnte aus einem älteren,
        # unbereinigten Tracker-Lauf stammen und wäre dann auf einer anderen
        # Skala als der frische, bereinigte Horizont-Kurs (derselbe
        # Split-Bug wie bei forward_tracker.py, siehe dort).
        baseline = float(closes.iloc[0])
        if price_at_snapshot is None:
            with get_conn() as conn:
                conn.execute(
                    "UPDATE scan_snapshots SET price_at_snapshot=? WHERE id=?",
                    (baseline, snapshot_id))

        for r in rows:
            # Zeile 0 = Snapshot-Tag; Horizont h = h Handelstage danach
            if len(closes) <= r["horizon_days"]:
                continue
            try:
                ret = (float(closes.iloc[r["horizon_days"]]) / baseline - 1) * 100
                with get_conn() as conn:
                    conn.execute(
                        "UPDATE scan_forward_returns SET ret_pct=?, filled_ts=? "
                        "WHERE snapshot_id=? AND horizon_days=?",
                        (round(ret, 2), now_iso, snapshot_id, r["horizon_days"]))
                filled += 1
            except Exception as e:
                log.warning("Scan-Tracker %s h=%d: %s", ticker, r["horizon_days"], e)
    log.info("Scan-Tracker: %d Returns gefüllt", filled)
