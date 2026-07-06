"""Layer 2: Volumen-Anomalie (z-Score) ohne News-Begleitung. Quelle: yfinance EOD.

Finnhub Free Tier liefert /stock/candle nicht mehr (403) -> yfinance als Primärquelle.
"""
import logging
import statistics
from datetime import datetime, timezone, date, timedelta

import yfinance as yf

from signals_db import get_conn, insert_signal

log = logging.getLogger("scanner")
CHUNK = 200            # Ticker pro yf.download-Call
MIN_HISTORY = 21       # 20 Tage Basis + heutiger Tag


def _news_flat(ticker: str) -> bool:
    """True wenn News-Count der letzten 3 Tage <= grober Toleranz ggü. dem Tagesmedian (30 Tage)."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT date, news_count FROM buzz_history WHERE ticker=? "
            "AND date >= date('now', '-30 days')", (ticker,)).fetchall()
    if not rows:
        return True  # keine News-Historie = keine News = flach
    counts = {r["date"]: r["news_count"] for r in rows}
    cutoff = (date.today() - timedelta(days=3)).isoformat()
    recent = sum(c for d, c in counts.items() if d >= cutoff)
    med = statistics.median(counts.values())
    return recent <= max(med * 3, 3)  # 3 Tage vs. Tagesmedian → Faktor 3


def run_volume_scan(cfg: dict) -> None:
    from scanner import _load_tickers
    z_min = cfg.get("early_signals", {}).get("volume_z_min", 2.5)
    tickers = [t["ticker"] for t in _load_tickers()]
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    hits = 0

    for i in range(0, len(tickers), CHUNK):
        chunk = tickers[i:i + CHUNK]
        try:
            data = yf.download(tickers=chunk, period="2mo", interval="1d",
                               group_by="ticker", threads=True, progress=False,
                               auto_adjust=False)
        except Exception as e:
            log.warning("yfinance Chunk %d: %s", i, e)
            continue
        for sym in chunk:
            try:
                vol = data[sym]["Volume"].dropna()
            except (KeyError, TypeError):
                continue
            if len(vol) < MIN_HISTORY:
                continue
            today_vol = float(vol.iloc[-1])
            base = [float(v) for v in vol.iloc[-21:-1]]
            mean_v = statistics.mean(base)
            sd_v = statistics.stdev(base)
            if sd_v == 0:
                continue
            z = (today_vol - mean_v) / sd_v
            if z < z_min or not _news_flat(sym):
                continue
            score = 3.0 if z >= 4.0 else 2.0
            insert_signal(sym, "volume_anomaly", now_iso, round(z, 2),
                          {"z_score": round(z, 2), "volume": today_vol,
                           "mean_20d": round(mean_v), "weight": score})
            hits += 1
    log.info("Volumen-Scan fertig: %d Anomalien", hits)
