import os
import csv
import json
import logging
import re
import time
import anthropic
import requests
from pathlib import Path
from datetime import datetime

log = logging.getLogger("scanner")

FINNHUB_KEY = os.environ.get("FINNHUB_API_KEY", "")
BASE = "https://finnhub.io/api/v1"
BASE_DIR = Path(__file__).parent
CALLS_PER_MIN = 55

HAIKU_PRICE_INPUT  = 1.00 / 1_000_000   # USD per input token (Haiku 4.5)
HAIKU_PRICE_OUTPUT = 5.00 / 1_000_000   # USD per output token (Haiku 4.5)
USD_TO_EUR = 0.92                         # Näherungswert, festgesetzt

_call_times: list[float] = []
_claude_client: anthropic.Anthropic | None = None


def _get_claude() -> anthropic.Anthropic:
    global _claude_client
    if _claude_client is None:
        _claude_client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))
    return _claude_client

# ── Scan-Status (thread-safe via GIL für einfache dict-Ops) ──────────────────
SCAN_STATUS: dict = {
    "running": False,
    "abort": False,
    "type": None,        # "full" | "portfolio"
    "started_at": None,
    "progress": 0,
    "total": 0,
    "current_ticker": "",
    "finished_at": None,
}

# ── Keyword-Sentiment ─────────────────────────────────────────────────────────

BULLISH_WORDS = {
    "beat", "beats", "record", "surge", "surges", "soar", "soars", "rally",
    "rallies", "raises", "raised", "upgrade", "upgraded", "upgrades",
    "outperform", "outperforms", "outperforming", "strong", "exceeds",
    "exceeded", "growth", "profit", "profits", "gains", "gain", "breakthrough",
    "launch", "launches", "launched", "partnership", "contract", "acquires",
    "acquisition", "buyback", "dividend", "positive", "bullish", "momentum",
    "higher", "increase", "increases", "increased", "expand", "expands",
    "expansion", "winning", "wins", "win", "success", "successful",
}

BEARISH_WORDS = {
    "miss", "misses", "missed", "loss", "losses", "cut", "cuts", "lower",
    "lowers", "downgrade", "downgraded", "downgrades", "underperform",
    "underperforms", "weak", "weakness", "lawsuit", "lawsuits",
    "investigation", "fraud", "bankruptcy", "default", "warning", "warns",
    "warned", "recall", "decline", "declines", "declined", "disappoints",
    "disappointed", "disappointing", "layoffs", "layoff", "restructuring",
    "shortfall", "negative", "bearish", "concern", "concerns", "delay",
    "delayed", "delays", "falling", "falls", "fell", "drops", "dropped",
    "slump", "slumps", "slumped", "plunges", "plunged",
}


def _score_text(text: str) -> int:
    words = set(text.lower().split())
    return len(words & BULLISH_WORDS) - len(words & BEARISH_WORDS)


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


def _load_portfolio() -> list[dict]:
    path = BASE_DIR / "portfolio.json"
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text())
    except Exception:
        return []


def _save_portfolio(data: list[dict]):
    path = BASE_DIR / "portfolio.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    tmp.rename(path)


def _calc_score(d: dict) -> float:
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


def _fetch_sentiment(ticker: str) -> dict | None:
    """Sentiment via /company-news + Keyword-Scoring. Gibt None bei Fehler."""
    from datetime import date, timedelta
    try:
        today = date.today().isoformat()
        week_ago = (date.today() - timedelta(days=7)).isoformat()
        news = _fh_get("/company-news", {"symbol": ticker, "from": week_ago, "to": today})
        if not isinstance(news, list):
            log.warning("%s sentiment: Finnhub-Antwort kein Array: %s", ticker, str(news)[:120])
            return None
        count = len(news)
        if count == 0:
            return {"buzz": 0.0, "articles_week": 0, "bullish_pct": 0.0,
                    "bearish_pct": 0.0, "sentiment_norm": 50.0, "_news_texts": []}
        scores = [
            _score_text((a.get("headline") or "") + " " + (a.get("summary") or ""))
            for a in news
        ]
        bullish_count = sum(1 for s in scores if s > 0)
        bearish_count = sum(1 for s in scores if s < 0)
        avg_score = sum(scores) / count
        # buzz: Artikel/Woche normiert (3 Artikel = 1.0 = "Durchschnitt")
        buzz = round(count / 3.0, 3)
        # sentiment_norm: 0–100 (avg_score ∈ [-3,+3] → linear auf 0–100)
        sentiment_norm = round(max(0.0, min(100.0, (avg_score + 3) / 6 * 100)), 1)
        return {
            "buzz": buzz,
            "articles_week": count,
            "bullish_pct": round(bullish_count / count * 100, 1),
            "bearish_pct": round(bearish_count / count * 100, 1),
            "sentiment_norm": sentiment_norm,
            "_news_texts": [
                ((a.get("headline") or "") + " " + (a.get("summary") or ""))[:200]
                for a in news[:8]
            ],
        }
    except Exception as e:
        log.warning("%s sentiment: %s", ticker, e)
        return None


def _fetch_quote(ticker: str) -> float | None:
    """Aktueller Kurs via /quote."""
    try:
        d = _fh_get("/quote", {"symbol": ticker})
        return d.get("c") or None
    except Exception:
        return None


def _check_sell_signal(entry: dict, curr: dict) -> tuple[bool, str | None]:
    """Gibt (sell_signal, reason) zurück. True nur wenn Stimmung JETZT gedreht ist."""
    prev = entry.get("last_sentiment")
    if not prev:
        return False, None  # Erstmalig gescannt → kein Signal

    prev_bullish = prev.get("bullish_pct", 0)
    prev_bearish = prev.get("bearish_pct", 0)
    prev_buzz = prev.get("buzz", 0)

    curr_bullish = curr.get("bullish_pct", 0)
    curr_bearish = curr.get("bearish_pct", 0)
    curr_buzz = curr.get("buzz", 0)

    # War bullish, jetzt nicht mehr (5-Punkte-Puffer)
    if prev_bullish >= 40 and curr_bullish < 35:
        return True, f"Bullish-Sentiment gefallen ({prev_bullish:.0f}% → {curr_bullish:.0f}%)"

    # Bearish-Stimmung gestiegen
    if prev_bearish <= 30 and curr_bearish > 40:
        return True, f"Bearish-Stimmung gestiegen ({prev_bearish:.0f}% → {curr_bearish:.0f}%)"

    # Buzz eingebrochen (war rising, jetzt fallend)
    if prev_buzz > 1.0 and curr_buzz < 0.7:
        return True, f"Buzz eingebrochen ({prev_buzz:.2f} → {curr_buzz:.2f})"

    return False, None


# ── Claude-Sentiment-Anreicherung ────────────────────────────────────────────

def _claude_enrich_batch(candidates: list[dict], token_acc: dict) -> None:
    """Ersetzt Keyword-Scores der Kandidaten durch Claude-Sentiment. Fallback: Keyword-Werte bleiben."""
    BATCH_SIZE = 10
    client = _get_claude()

    for i in range(0, len(candidates), BATCH_SIZE):
        batch = [c for c in candidates[i:i + BATCH_SIZE] if c.get("_news_texts")]
        if not batch:
            continue

        lines = [
            "Analysiere das Aktien-Sentiment anhand der folgenden Nachrichtentexte.\n"
            "Antworte NUR mit einem JSON-Array, kein erklärender Text.\n"
            "Felder je Ticker: ticker, bullish_pct (0-100), bearish_pct (0-100), "
            "sentiment_norm (0-100), confidence (0-100)\n"
        ]
        for c in batch:
            lines.append(f"\n[{c['ticker']}]")
            for txt in c["_news_texts"][:5]:
                lines.append(f"- {txt[:150]}")

        try:
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=1024,
                messages=[{"role": "user", "content": "\n".join(lines)}],
            )
            token_acc["input_tokens"]  += resp.usage.input_tokens
            token_acc["output_tokens"] += resp.usage.output_tokens

            m = re.search(r'\[.*?\]', resp.content[0].text.strip(), re.DOTALL)
            if not m:
                log.warning("Claude Batch %d: kein JSON in Antwort", i // BATCH_SIZE)
                continue

            result_map = {r["ticker"]: r for r in json.loads(m.group()) if "ticker" in r}
            for c in batch:
                if c["ticker"] in result_map:
                    r = result_map[c["ticker"]]
                    c["bullish_pct"]       = round(float(r.get("bullish_pct",    c["bullish_pct"])),    1)
                    c["bearish_pct"]       = round(float(r.get("bearish_pct",    c["bearish_pct"])),    1)
                    c["sentiment_norm"]    = round(float(r.get("sentiment_norm", c["sentiment_norm"])), 1)
                    c["claude_confidence"] = int(r.get("confidence", 0))
        except Exception as e:
            log.warning("Claude Batch %d Fehler: %s", i // BATCH_SIZE, e)


def _update_claude_costs(token_acc: dict) -> None:
    """Summiert Kosten in claude_costs.json und sendet Telegram-Alert bei neuem €1-Schwellenwert."""
    cost_usd = (
        token_acc["input_tokens"]  * HAIKU_PRICE_INPUT +
        token_acc["output_tokens"] * HAIKU_PRICE_OUTPUT
    )
    cost_eur = cost_usd * USD_TO_EUR

    path = BASE_DIR / "claude_costs.json"
    if path.exists():
        data = json.loads(path.read_text())
    else:
        data = {
            "total_cost_eur": 0.0, "total_cost_usd": 0.0,
            "total_input_tokens": 0, "total_output_tokens": 0,
            "last_threshold_notified": 0, "scans": [],
        }

    data["total_input_tokens"]  += token_acc["input_tokens"]
    data["total_output_tokens"] += token_acc["output_tokens"]
    data["total_cost_usd"] = round(data["total_cost_usd"] + cost_usd, 6)
    data["total_cost_eur"] = round(data["total_cost_eur"] + cost_eur, 6)
    data["scans"].append({
        "scanned_at":         datetime.utcnow().isoformat() + "Z",
        "input_tokens":       token_acc["input_tokens"],
        "output_tokens":      token_acc["output_tokens"],
        "candidates_analyzed": token_acc.get("candidates", 0),
        "cost_usd":           round(cost_usd, 6),
        "cost_eur":           round(cost_eur, 6),
    })

    new_threshold = int(data["total_cost_eur"])
    if new_threshold > data["last_threshold_notified"]:
        _tg_post(
            f"💰 <b>Claude API: {new_threshold} € kumulativ</b>\n"
            f"Sentiment Scanner: {data['total_cost_eur']:.4f} €\n"
            f"({data['total_input_tokens']:,} Input + {data['total_output_tokens']:,} Output Tokens)"
        )
        data["last_threshold_notified"] = new_threshold

    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    tmp.rename(path)


# ── Voller Scan (alle Ticker) ─────────────────────────────────────────────────

def run_scan(cfg: dict) -> dict:
    global SCAN_STATUS
    if SCAN_STATUS.get("running"):
        log.warning("run_scan aufgerufen während Scan läuft – abgebrochen")
        return {}
    SCAN_STATUS.update({
        "running": True, "type": "full",
        "started_at": datetime.utcnow().isoformat() + "Z",
        "progress": 0, "total": 0,
        "current_ticker": "", "finished_at": None,
    })

    f = cfg["filter"]
    tickers = _load_tickers()
    portfolio = _load_portfolio()
    portfolio_tickers = {p["ticker"] for p in portfolio}

    log.info("Vollständiger Scan gestartet: %d Ticker", len(tickers))
    SCAN_STATUS["total"] = len(tickers)

    # Stufe 1: news-sentiment → Sentiment-Filter
    candidates: list[dict] = []
    all_scanned: dict[str, dict] = {}  # ticker → sentiment (auch verworfene)
    sent_errors = 0
    consecutive_errors = 0
    EARLY_ABORT_THRESHOLD = 50  # Abbruch bei 50 API-Fehlern in Folge

    for i, t in enumerate(tickers):
        if SCAN_STATUS.get("abort"):
            log.info("Scan durch Benutzer abgebrochen bei %d/%d", i, len(tickers))
            break
        SCAN_STATUS["progress"] = i + 1
        SCAN_STATUS["current_ticker"] = t["ticker"]

        sent = _fetch_sentiment(t["ticker"])
        if sent is None:
            sent_errors += 1
            consecutive_errors += 1
            if consecutive_errors >= EARLY_ABORT_THRESHOLD:
                log.error("Scan abgebrochen: %d aufeinanderfolgende API-Fehler – Finnhub-Key oder Tageslimit prüfen!", consecutive_errors)
                SCAN_STATUS["abort"] = True
                break
            continue

        consecutive_errors = 0
        all_scanned[t["ticker"]] = {**t, **sent}

        # Filter anwenden
        buzz_val = sent["buzz"]
        if f.get("buzz_trend_rising") and buzz_val <= 1.0:
            continue
        if sent["bullish_pct"] < f["bullish_pct_min"]:
            continue
        if sent["bearish_pct"] > f["bearish_pct_max"]:
            continue
        if sent["articles_week"] < f["news_min_count"]:
            continue

        candidates.append({**t, **sent})

    log.info("Sentiment-Filter: %d Kandidaten, %d API-Fehler", len(candidates), sent_errors)

    # Claude-Anreicherung (nur Kandidaten, nur wenn API-Key gesetzt)
    scan_tokens = {"input_tokens": 0, "output_tokens": 0, "candidates": len(candidates)}
    if candidates and os.environ.get("ANTHROPIC_API_KEY"):
        _claude_enrich_batch(candidates, scan_tokens)
        log.info("Claude-Analyse: %d Input-, %d Output-Tokens",
                 scan_tokens["input_tokens"], scan_tokens["output_tokens"])

    # _news_texts aus allen Dicts entfernen (nicht in results.json speichern)
    for c in candidates:
        c.pop("_news_texts", None)
    for v in all_scanned.values():
        v.pop("_news_texts", None)

    # Stufe 2: MarketCap-Filter (nur für Kandidaten)
    valid: list[dict] = []
    mc_errors = 0
    for c in candidates:
        SCAN_STATUS["current_ticker"] = c["ticker"]
        try:
            m = _fh_get("/stock/metric", {"symbol": c["ticker"], "metric": "all"})
            metrics = m.get("metric") or {}
            mc_m = metrics.get("marketCapitalization")
            pe = metrics.get("peNormalizedAnnual")

            if mc_m is not None:
                mc_usd = mc_m * 1_000_000
                if not (f["market_cap_min_usd"] <= mc_usd <= f["market_cap_max_usd"]):
                    continue
                c["market_cap"] = int(mc_usd)
            else:
                c["market_cap"] = None

            c["pe"] = pe
            valid.append(c)
        except Exception as e:
            mc_errors += 1
            log.debug("%s metric: %s", c["ticker"], e)

    log.info("MarketCap-Filter: %d Treffer, %d Fehler", len(valid), mc_errors)

    for v in valid:
        v["score"] = _calc_score(v)

    valid.sort(key=lambda x: x["score"], reverse=True)
    top_n = valid[: cfg.get("top_n_results", 50)]
    top_tickers = {r["ticker"] for r in top_n}

    # Portfolio-Aktien immer in Top N aufnehmen (pinned)
    for pt in portfolio_tickers:
        if pt in top_tickers:
            # Schon drin → als pinned markieren
            for r in top_n:
                if r["ticker"] == pt:
                    r["pinned"] = True
        else:
            # Nicht in Top N → forcieren, Daten aus all_scanned oder neu holen
            base = all_scanned.get(pt)
            if base is None:
                sent = _fetch_sentiment(pt)
                if sent:
                    pname = next((p["name"] for p in portfolio if p["ticker"] == pt), pt)
                    base = {"ticker": pt, "name": pname, **sent}
            if base:
                try:
                    m = _fh_get("/stock/metric", {"symbol": pt, "metric": "all"})
                    metrics = m.get("metric") or {}
                    mc_m = metrics.get("marketCapitalization")
                    base["market_cap"] = int(mc_m * 1_000_000) if mc_m else None
                    base["pe"] = metrics.get("peNormalizedAnnual")
                except Exception:
                    base["market_cap"] = None
                    base["pe"] = None
                base["score"] = _calc_score(base)
                base["pinned"] = True
                top_n.append(base)

    output = {
        "scanned_at": datetime.utcnow().isoformat() + "Z",
        "scanned_count": len(tickers),
        "candidates_count": len(candidates),
        "results_count": len(valid),
        "errors": sent_errors,
        "results": top_n,
    }
    _write_results(output)

    # Portfolio-Quote und Wert aktualisieren
    _update_portfolio_quotes(portfolio)

    log.info("Vollständiger Scan fertig – %d Ergebnisse", len(top_n))
    _send_telegram_top5(top_n[:5], len(tickers))

    if scan_tokens["input_tokens"] > 0:
        _update_claude_costs(scan_tokens)

    SCAN_STATUS.update({
        "running": False,
        "abort": False,
        "finished_at": datetime.utcnow().isoformat() + "Z",
        "current_ticker": "",
    })
    return output


# ── Portfolio-Schnell-Scan ────────────────────────────────────────────────────

def run_portfolio_scan() -> None:
    """Nur Portfolio-Aktien scannen, Alert bei gedrehter Stimmung."""
    global SCAN_STATUS
    portfolio = _load_portfolio()
    if not portfolio:
        return

    SCAN_STATUS.update({
        "running": True, "type": "portfolio",
        "started_at": datetime.utcnow().isoformat() + "Z",
        "progress": 0, "total": len(portfolio),
        "current_ticker": "", "finished_at": None,
    })

    log.info("Portfolio-Scan gestartet: %d Aktien", len(portfolio))
    changed = False

    for i, entry in enumerate(portfolio):
        ticker = entry["ticker"]
        SCAN_STATUS["progress"] = i + 1
        SCAN_STATUS["current_ticker"] = ticker

        sent = _fetch_sentiment(ticker)
        price = _fetch_quote(ticker)

        if sent is None:
            continue

        # Sell-Signal prüfen (nur wenn Signal noch nicht aktiv)
        if not entry.get("sell_signal"):
            signal, reason = _check_sell_signal(entry, sent)
            if signal:
                entry["sell_signal"] = True
                entry["sell_reason"] = reason
                log.info("SELL-SIGNAL %s: %s", ticker, reason)
                _send_telegram_sell(entry, sent, price, reason)
                changed = True
        else:
            # Signal zurücksetzen wenn Stimmung wieder gut
            if sent["bullish_pct"] >= 40 and sent["bearish_pct"] <= 30:
                entry["sell_signal"] = False
                entry["sell_reason"] = None
                changed = True

        # last_sentiment aktualisieren
        entry["last_sentiment"] = {
            **sent,
            "price": price,
            "scanned_at": datetime.utcnow().isoformat() + "Z",
        }
        if price:
            entry["current_price"] = price
            entry["current_value"] = round(price * entry.get("shares", 0), 2)
            cost = entry.get("buy_price", 0) * entry.get("shares", 0)
            entry["pnl"] = round(entry["current_value"] - cost, 2)
            entry["pnl_pct"] = round((entry["pnl"] / cost * 100) if cost else 0, 2)
        changed = True

    if changed:
        _save_portfolio(portfolio)

    SCAN_STATUS.update({
        "running": False,
        "finished_at": datetime.utcnow().isoformat() + "Z",
        "current_ticker": "",
    })
    log.info("Portfolio-Scan fertig")


def _update_portfolio_quotes(portfolio: list[dict]) -> None:
    """Kurse und P&L nach vollständigem Scan aktualisieren."""
    if not portfolio:
        return
    changed = False
    for entry in portfolio:
        price = _fetch_quote(entry["ticker"])
        if price:
            entry["current_price"] = price
            entry["current_value"] = round(price * entry.get("shares", 0), 2)
            cost = entry.get("buy_price", 0) * entry.get("shares", 0)
            entry["pnl"] = round(entry["current_value"] - cost, 2)
            entry["pnl_pct"] = round((entry["pnl"] / cost * 100) if cost else 0, 2)
            changed = True
    if changed:
        _save_portfolio(portfolio)


# ── Datei-Helfer ──────────────────────────────────────────────────────────────

def _write_results(data: dict):
    path = BASE_DIR / "results.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    tmp.rename(path)


# ── Telegram ──────────────────────────────────────────────────────────────────

def _tg_post(text: str):
    token = os.environ.get("TOKEN", "")
    chat_id = os.environ.get("CHAT_ID", "")
    if not token or not chat_id:
        log.warning("Telegram: TOKEN oder CHAT_ID fehlt")
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=15,
        )
    except Exception as e:
        log.warning("Telegram-Fehler: %s", e)


def _send_telegram_top5(top5: list, scanned: int):
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
    lines += ["", f"ℹ️ {scanned} Ticker gescannt | Finnhub Free API",
              "⚠️ Kein Investment-Advice. Nur Sentiment-Daten."]
    _tg_post("\n".join(lines))


def _send_telegram_sell(entry: dict, sent: dict, price: float | None, reason: str):
    ticker = entry["ticker"]
    name = entry.get("name", ticker)
    shares = entry.get("shares", 0)
    buy_price = entry.get("buy_price", 0)
    curr_val = f"${price * shares:.2f}" if price else "–"
    pnl = entry.get("pnl")
    pnl_str = f"{'+'if pnl >= 0 else ''}{pnl:.2f} USD" if pnl is not None else "–"

    text = (
        f"🔴 <b>VERKAUFSEMPFEHLUNG: {ticker}</b>\n"
        f"{name}\n\n"
        f"<b>Grund:</b> {reason}\n\n"
        f"Bullish: {sent['bullish_pct']}% | Bearish: {sent['bearish_pct']}% | Buzz: {sent['buzz']:.2f}\n"
        f"Aktueller Kurs: {'$'+f'{price:.2f}' if price else '–'}\n"
        f"Positionswert: {curr_val} | P&L: {pnl_str}\n\n"
        f"⚠️ Kein Investment-Advice. Nur Sentiment-Daten."
    )
    _tg_post(text)
    log.info("Telegram Sell-Alert gesendet: %s", ticker)
