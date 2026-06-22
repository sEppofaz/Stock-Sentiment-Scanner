import os
import csv
import json
import logging
import time
import requests
from pathlib import Path
from datetime import datetime

log = logging.getLogger("scanner")

FINNHUB_KEY = os.environ.get("FINNHUB_API_KEY", "")
BASE = "https://finnhub.io/api/v1"
BASE_DIR = Path(__file__).parent
CALLS_PER_MIN = 55

_call_times: list[float] = []


def _throttle():
    now = time.time()
    global _call_times
    _call_times = [t for t in _call_times if now - t < 60]
    if len(_call_times) >= CALLS_PER_MIN:
        wait = 61 - (now - _call_times[0])
        if wait > 0:
            time.sleep(wait)
            _call_times = []
    _call_times.append(time.time())


def _fh_get(path: str, params: dict | None = None) -> dict:
    _throttle()
    p = {**(params or {}), "token": FINNHUB_KEY}
    r = requests.get(f"{BASE}{path}", params=p, timeout=15)
    r.raise_for_status()
    return r.json()


def _load_tickers() -> list[dict]:
    rows = []
    with open(BASE_DIR / "tickers.csv", newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            ticker = row.get("ticker", "").strip()
            name = row.get("name", "").strip()
            if ticker:
                rows.append({"ticker": ticker, "name": name})
    return rows


def _calc_score(d: dict) -> float:
    # buzz ist ein Ratio (>1 = über Jahresdurchschnitt); clamp auf 0-100
    buzz_norm = min(d.get("buzz", 0) * 33.3, 100)
    base = (
        0.45 * d.get("bullish_pct", 0)
        + 0.30 * buzz_norm
        + 0.25 * d.get("sentiment_norm", 0)
    )
    pe = d.get("pe")
    if pe and 0 < pe < 30:
        base += max(0, (30 - pe) / 30 * 10)
    return round(min(base, 100), 2)


def run_scan(cfg: dict) -> dict:
    f = cfg["filter"]
    tickers = _load_tickers()
    log.info("Scan gestartet: %d Ticker", len(tickers))

    # Stufe 1: news-sentiment → Buzz + Bullish + News-Volumen
    candidates: list[dict] = []
    errors = 0

    for t in tickers:
        try:
            d = _fh_get("/news-sentiment", {"symbol": t["ticker"]})
            buzz_obj = d.get("buzz") or {}
            sent_obj = d.get("sentiment") or {}

            buzz_val = buzz_obj.get("buzz", 0) or 0
            articles = buzz_obj.get("articlesInLastWeek", 0) or 0
            bullish = (sent_obj.get("bullishPercent") or 0) * 100
            bearish = (sent_obj.get("bearishPercent") or 0) * 100
            news_score = (d.get("companyNewsScore") or 0) * 100

            if f.get("buzz_trend_rising") and buzz_val <= 1.0:
                continue
            if bullish < f["bullish_pct_min"]:
                continue
            if bearish > f["bearish_pct_max"]:
                continue
            if articles < f["news_min_count"]:
                continue

            candidates.append({
                "ticker": t["ticker"],
                "name": t["name"],
                "buzz": round(buzz_val, 3),
                "bullish_pct": round(bullish, 1),
                "bearish_pct": round(bearish, 1),
                "articles_week": articles,
                "sentiment_norm": round(news_score, 1),
            })
        except Exception as e:
            errors += 1
            log.debug("%s: %s", t["ticker"], e)

    log.info("Sentiment-Filter: %d Kandidaten", len(candidates))

    # Stufe 2: stock/metric → MarketCap-Filter (nur für Kandidaten)
    valid: list[dict] = []
    for c in candidates:
        try:
            m = _fh_get("/stock/metric", {"symbol": c["ticker"], "metric": "all"})
            metrics = m.get("metric") or {}
            mc_m = metrics.get("marketCapitalization")  # Finnhub: Millionen USD
            pe = metrics.get("peNormalizedAnnual")

            if mc_m is not None:
                mc_usd = mc_m * 1_000_000
                if not (f["market_cap_min_usd"] <= mc_usd <= f["market_cap_max_usd"]):
                    continue
                c["market_cap"] = int(mc_usd)
            else:
                c["market_cap"] = None  # unbekannt → nicht ausschließen

            c["pe"] = pe
            valid.append(c)
        except Exception as e:
            errors += 1
            log.debug("%s metric: %s", c["ticker"], e)

    log.info("MarketCap-Filter: %d Treffer, %d Fehler", len(valid), errors)

    for v in valid:
        v["score"] = _calc_score(v)

    valid.sort(key=lambda x: x["score"], reverse=True)
    top50 = valid[: cfg.get("top_n_results", 50)]

    output = {
        "scanned_at": datetime.utcnow().isoformat() + "Z",
        "scanned_count": len(tickers),
        "candidates_count": len(candidates),
        "results_count": len(valid),
        "errors": errors,
        "results": top50,
    }
    _write_results(output)
    log.info("Scan fertig – Top %d gespeichert", len(top50))

    _send_telegram(top50[:5], len(tickers))
    return output


def _write_results(data: dict):
    path = BASE_DIR / "results.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    tmp.rename(path)


def _send_telegram(top5: list, scanned: int):
    token = os.environ.get("TOKEN", "")
    chat_id = os.environ.get("CHAT_ID", "")
    if not token or not chat_id:
        log.warning("Telegram: TOKEN oder CHAT_ID fehlt")
        return

    now = datetime.utcnow().strftime("%H:%M UTC")
    lines = [f"<b>📊 Stock Sentiment Scan — {now}</b>", ""]
    lines.append("🟢 <b>TOP 5 Small Caps (Rising Sentiment)</b>")
    lines.append("")

    for i, r in enumerate(top5, 1):
        mc = f"${r['market_cap'] // 1_000_000}M" if r.get("market_cap") else "–"
        lines.append(
            f"{i}. <b>{r['ticker']}</b> — {r['name']}\n"
            f"   Score: {r['score']} | Bullish: {r['bullish_pct']}% | Buzz: {r['buzz']:.2f}↑\n"
            f"   MarketCap: {mc} | {r['articles_week']} News (7d)"
        )

    lines.append("")
    lines.append(f"ℹ️ {scanned} Ticker gescannt | Finnhub Free API")
    lines.append("⚠️ Kein Investment-Advice. Nur Sentiment-Daten.")

    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": "\n".join(lines),
                "parse_mode": "HTML",
            },
            timeout=15,
        )
        log.info("Telegram-Nachricht gesendet")
    except Exception as e:
        log.warning("Telegram-Fehler: %s", e)
