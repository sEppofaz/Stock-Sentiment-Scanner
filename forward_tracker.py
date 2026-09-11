"""Füllt forward_returns wenn der Handelstage-Horizont (1/5/20) erreicht ist."""
import logging
from datetime import datetime, timezone

from signals_db import get_conn
from yf_helper import fetch_closes

log = logging.getLogger("scanner")

_BENCHMARKS = ("IWM", "SPY")


def _get_benchmark_closes(symbol: str, start_date: str, cache: dict):
    """Cached pro (symbol, start_date) innerhalb EINES Tracker-Laufs – mehrere
    Alerts vom selben Tag teilen sich denselben Benchmark-Download statt ihn
    mehrfach zu wiederholen (analog G7-Fix, aber für IWM/SPY statt den
    Ticker selbst, Fable-Review 2026-09-11)."""
    key = (symbol, start_date)
    if key not in cache:
        cache[key] = fetch_closes(symbol, start_date)
    return cache[key]


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

    bench_cache: dict[tuple, object] = {}
    filled = 0
    for (ticker, alert_id), rows in by_alert.items():
        alert_ts = rows[0]["alert_ts"]
        closes = fetch_closes(ticker, alert_ts[:10])
        if closes is None:
            continue
        bench_closes = {sym: _get_benchmark_closes(sym, alert_ts[:10], bench_cache)
                         for sym in _BENCHMARKS}
        # Referenzkurs kommt seit 2026-08-21 aus derselben (jetzt Split-
        # bereinigten) yfinance-Reihe wie der Horizont-Kurs, NICHT mehr aus
        # der separat gespeicherten Finnhub-Live-Quote (price_at_alert) - die
        # bleibt unverändert als reine Anzeige-Info erhalten (echter Kurs zum
        # Alert-Zeitpunkt), wird aber nicht mehr für die Rendite verwendet.
        # Grund: Ein Split zwischen Alert und Horizont hätte sonst Zähler
        # (bereinigt) und Nenner (unbereinigt) auf unterschiedliche Skalen
        # gebracht - derselbe Bug wie bei auto_adjust=False, nur umgekehrt.
        baseline = float(closes.iloc[0])
        for r in rows:
            # Zeile 0 = Alert-Tag; Horizont h = h Handelstage danach
            if len(closes) <= r["horizon_days"]:
                continue
            try:
                ret = (float(closes.iloc[r["horizon_days"]]) / baseline - 1) * 100
                bench_rets = {}
                for sym, bc in bench_closes.items():
                    if bc is not None and len(bc) > r["horizon_days"]:
                        bench_rets[sym] = round(
                            (float(bc.iloc[r["horizon_days"]]) / float(bc.iloc[0]) - 1) * 100, 2)
                    else:
                        bench_rets[sym] = None
                with get_conn() as conn:
                    conn.execute(
                        "UPDATE forward_returns SET ret_pct=?, filled_ts=?, "
                        "benchmark_iwm_ret_pct=?, benchmark_spy_ret_pct=? "
                        "WHERE alert_id=? AND horizon_days=?",
                        (round(ret, 2), now_iso, bench_rets["IWM"], bench_rets["SPY"],
                         alert_id, r["horizon_days"]))
                filled += 1
            except Exception as e:
                log.warning("Tracker %s h=%d: %s", ticker, r["horizon_days"], e)
    log.info("Forward-Tracker: %d Returns gefüllt", filled)
