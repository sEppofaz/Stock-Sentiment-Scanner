"""Füllt forward_returns wenn der Handelstage-Horizont (1/5/20) erreicht ist."""
import logging
from datetime import datetime, timezone

import yfinance as yf

from signals_db import get_conn

log = logging.getLogger("scanner")


def run_tracker(cfg: dict) -> None:
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with get_conn() as conn:
        open_rows = conn.execute(
            "SELECT fr.alert_id, fr.horizon_days, a.ticker, a.alert_ts, a.price_at_alert "
            "FROM forward_returns fr JOIN alerts a ON a.id = fr.alert_id "
            "WHERE fr.ret_pct IS NULL AND a.price_at_alert IS NOT NULL").fetchall()

    # Nach (ticker, alert_id) gruppieren – ein yfinance-Call pro Alert statt bis
    # zu drei identischer Downloads (je einer pro Horizont 1/5/20) (G7)
    by_alert: dict[tuple, list] = {}
    for r in open_rows:
        by_alert.setdefault((r["ticker"], r["alert_id"]), []).append(r)

    filled = 0
    for (ticker, alert_id), rows in by_alert.items():
        alert_ts = rows[0]["alert_ts"]
        price_at_alert = rows[0]["price_at_alert"]
        try:
            hist = yf.download(ticker, start=alert_ts[:10], interval="1d",
                               progress=False, auto_adjust=False)
            # yfinance liefert auch bei einzelnem String-Ticker MultiIndex-Spalten
            # (verifiziert 2026-07-06) -> hist["Close"] ist ein DataFrame, kein Series.
            closes = hist["Close"][ticker].dropna()
        except Exception as e:
            log.warning("Tracker %s: %s", ticker, e)
            continue
        for r in rows:
            # Zeile 0 = Alert-Tag; Horizont h = h Handelstage danach
            if len(closes) <= r["horizon_days"]:
                continue
            try:
                ret = (float(closes.iloc[r["horizon_days"]]) / price_at_alert - 1) * 100
                with get_conn() as conn:
                    conn.execute(
                        "UPDATE forward_returns SET ret_pct=?, filled_ts=? "
                        "WHERE alert_id=? AND horizon_days=?",
                        (round(ret, 2), now_iso, alert_id, r["horizon_days"]))
                filled += 1
            except Exception as e:
                log.warning("Tracker %s h=%d: %s", ticker, r["horizon_days"], e)
    log.info("Forward-Tracker: %d Returns gefüllt", filled)
