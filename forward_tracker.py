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
    filled = 0
    for r in open_rows:
        try:
            hist = yf.download(r["ticker"], start=r["alert_ts"][:10], interval="1d",
                               progress=False, auto_adjust=False)
            # yfinance liefert auch bei einzelnem String-Ticker MultiIndex-Spalten
            # (verifiziert 2026-07-06) -> hist["Close"] ist ein DataFrame, kein Series.
            closes = hist["Close"][r["ticker"]].dropna()
            # Zeile 0 = Alert-Tag; Horizont h = h Handelstage danach
            if len(closes) <= r["horizon_days"]:
                continue
            ret = (float(closes.iloc[r["horizon_days"]]) / r["price_at_alert"] - 1) * 100
            with get_conn() as conn:
                conn.execute(
                    "UPDATE forward_returns SET ret_pct=?, filled_ts=? "
                    "WHERE alert_id=? AND horizon_days=?",
                    (round(ret, 2), now_iso, r["alert_id"], r["horizon_days"]))
            filled += 1
        except Exception as e:
            log.warning("Tracker %s h=%d: %s", r["ticker"], r["horizon_days"], e)
    log.info("Forward-Tracker: %d Returns gefüllt", filled)
