"""Layer 3: Buzz-Beschleunigung aus buzz_history (keine API-Calls)."""
import logging
from datetime import datetime, timezone, date, timedelta

from signals_db import get_conn, insert_signal

log = logging.getLogger("scanner")


def run_buzz_accel(cfg: dict) -> None:
    es = cfg.get("early_signals", {})
    rel_min = es.get("buzz_rel_accel_min", 1.0)
    news_min = cfg.get("filter", {}).get("news_min_count", 3)
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    d = date.today()
    win_recent = [(d - timedelta(days=k)).isoformat() for k in range(0, 3)]   # d0..d2
    win_prev = [(d - timedelta(days=k)).isoformat() for k in range(3, 6)]     # d3..d5
    hits = 0

    with get_conn() as conn:
        tickers = [r["ticker"] for r in conn.execute(
            "SELECT DISTINCT ticker FROM buzz_history WHERE date >= date('now','-6 days')")]
        for t in tickers:
            rows = dict(conn.execute(
                "SELECT date, news_count FROM buzz_history WHERE ticker=? "
                "AND date >= date('now','-6 days')", (t,)).fetchall())
            recent = sum(rows.get(day, 0) for day in win_recent)
            prev = sum(rows.get(day, 0) for day in win_prev)
            rel = (recent - prev) / max(1, prev)
            weekly = recent + prev
            # feuert nur UNTER dem bestehenden Kandidaten-Level und mit Mindestsubstanz
            if rel >= rel_min and recent >= 3 and weekly <= news_min * 3:
                insert_signal(t, "buzz_accel", now_iso, round(rel, 2),
                              {"recent_3d": recent, "prev_3d": prev, "rel_accel": round(rel, 2)})
                hits += 1
    log.info("Buzz-Accel fertig: %d Signale", hits)
