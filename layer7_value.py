"""Layer 7: Value/Quality-Screening auf fundamentale Unterbewertung.

Nutzt denselben /stock/metric-Endpoint wie der Vollscan (scanner.py, Stufe 2),
zieht aber zusaetzliche, bislang ungenutzte Felder aus derselben Antwort.
Feldverfuegbarkeit wurde 2026-09-12 gegen Finnhub Free Tier verifiziert
(AAPL/F/KSS/PLUG/GPRO, siehe PKA-Logbuch):
- roicTTM existiert NICHT -> roeTTM als primaere Profitabilitaets-Kennzahl.
- evEbitda existiert NICHT -> currentEv/freeCashFlowTTM (EV/FCF) als Ersatz,
  methodisch sogar robuster (schwerer bilanziell zu schoenen als EBITDA, da
  Capex eingerechnet ist).
- Piotroski-F-Score nur als REDUZIERTER Proxy moeglich: Finnhub /stock/metric
  liefert nur den aktuellen Wert, keine Zeitreihe -> YoY-Trend-Kriterien
  (z.B. "ROA steigend ggue. Vorjahr") sind nicht pruefbar. _f_score_reduced()
  gibt deshalb (erreichte_punkte, verfuegbare_kriterien) zurueck statt eines
  vollen 0-9-Scores, und ist reine Zusatzinfo in details_json, NICHT Teil des
  Rankings.
- Kein voller Altman-Z-Score moeglich (keine rohen Bilanzgroessen aus dem
  Free Tier) -> _distress_veto() als grobes Ausschlusskriterium statt einer
  vorgetaeuschten Kennzahl.

Laeuft bewusst NICHT im werktaeglichen 15-Min-Raster der anderen Layer:
Fundamentaldaten aendern sich nicht untertaegig, und ein Scan ueber alle
~4700 Ticker braucht selbst beim gemeinsamen 55-Calls/Min-Throttle
(scanner._throttle()) ca. 85 Minuten - das wuerde mit Vollscan/Frueh-
signal-Jobs um dasselbe Budget konkurrieren. Siehe app.py-Scheduler:
eigener wochenendlicher Slot (Samstag), an dem sonst kein Finnhub-Job laeuft.

Scoring ist Rang-basiert (Value-Rang + Quality-Rang, je 0-100 ueber die
gefilterte Kandidatenmenge, kombiniert 50/50) statt additiver Bonuspunkte
wie beim bestehenden KGV-Bonus in scanner._calc_score() - eine einzelne
extreme Kennzahl kann den Gesamt-Score dadurch nicht dominieren.
"""
import logging
from datetime import datetime, timezone

from signals_db import insert_signal

log = logging.getLogger("scanner")

# Analog scanner.SCAN_STATUS: eigener Status-Dict, da ein Value-Scan (~85 Min
# über alle Ticker) sowohl vom wöchentlichen Cron-Job als auch manuell per
# API-Trigger gestartet werden kann - verhindert Überlappung und liefert der
# PWA (spaeter, Phase 4) einen Fortschritts-Endpoint wie /api/scan/status.
VALUE_SCAN_STATUS: dict = {
    "running": False,
    "started_at": None,
    "progress": 0,
    "total": 0,
    "current_ticker": "",
    "finished_at": None,
    "candidates_found": 0,
}

# (Feldname in _extract_metrics()-Ergebnis, higher_is_better)
_VALUE_FIELDS = [("pe", False), ("pb", False), ("ev_fcf", False)]
_QUALITY_FIELDS = [("roe", True), ("current_ratio", True), ("debt_equity", False),
                    ("revenue_growth", True)]
_CORE_FIELDS = ["pe", "pb", "roe", "debt_equity", "current_ratio", "ev_fcf"]
_MIN_CORE_FIELDS = 4  # von 6 - verhindert Scores auf duennster Datenbasis


def _extract_metrics(raw: dict) -> dict:
    """Liest die fuer den Value-Layer relevanten Felder aus einer /stock/metric-
    Antwort. Fehlende Felder bleiben None (kein 0-Ersatz - eine fehlende
    Kennzahl ist keine schlechte Kennzahl)."""
    return {
        "pe": raw.get("peNormalizedAnnual"),
        "pb": raw.get("pbAnnual"),
        "roe": raw.get("roeTTM"),
        "debt_equity": raw.get("totalDebt/totalEquityAnnual"),
        "current_ratio": raw.get("currentRatioAnnual"),
        "ev_fcf": raw.get("currentEv/freeCashFlowTTM"),
        "net_margin": raw.get("netProfitMarginTTM"),
        "revenue_growth": raw.get("revenueGrowthTTMYoy"),
    }


def _f_score_reduced(m: dict) -> tuple[int, int]:
    """Reduzierter Piotroski-Proxy ohne YoY-Trend-Kriterien (siehe Modul-
    Docstring). Rueckgabe: (erreichte Punkte, verfuegbare Kriterien) - NIE
    als vollwertigen 0-9-F-Score ausgeben."""
    checks = [
        (m.get("net_margin"), lambda v: v > 0),
        (m.get("roe"), lambda v: v > 0),
        (m.get("current_ratio"), lambda v: v > 1.0),
        (m.get("debt_equity"), lambda v: v < 1.5),
        (m.get("revenue_growth"), lambda v: v > 0),
    ]
    available = [v for v, _ in checks if v is not None]
    hits = [v for v, test in checks if v is not None and test(v)]
    return len(hits), len(available)


def _distress_veto(m: dict) -> bool:
    """Grobes Distress-Veto statt eines vollen Altman-Z-Scores (Finnhub Free
    Tier liefert keine rohen Bilanzgroessen dafuer): Current Ratio < 1 UND
    negative ROE deuten auf akute Liquiditaets-/Ertragsprobleme hin -
    'billig, aber wahrscheinlich aus gutem Grund' statt Value-Chance."""
    cr, roe = m.get("current_ratio"), m.get("roe")
    return cr is not None and roe is not None and cr < 1.0 and roe < 0


def _percentile_ranks(values: dict, higher_is_better: bool) -> dict:
    """0-100 je Ticker, 100 = am besten unter den uebergebenen Werten. Nur
    ueber Ticker, bei denen die Kennzahl vorhanden ist."""
    items = sorted(values.items(), key=lambda kv: kv[1], reverse=higher_is_better)
    n = len(items)
    if n == 0:
        return {}
    if n == 1:
        return {items[0][0]: 50.0}
    return {t: round((n - 1 - i) / (n - 1) * 100, 2) for i, (t, _) in enumerate(items)}


def run_value_scan(cfg: dict) -> None:
    from scanner import _fh_get, _load_tickers

    if VALUE_SCAN_STATUS["running"]:
        log.info("Value-Scan übersprungen – läuft bereits (Cron+manueller Trigger überlappt?)")
        return

    vl = cfg.get("value_layer", {})
    mc_min = vl.get("market_cap_min_usd", 50_000_000)
    mc_max = vl.get("market_cap_max_usd")  # None = kein Deckel nach oben
    pe_max = vl.get("pe_max")
    pb_max = vl.get("pb_max")
    roe_min = vl.get("roe_min")
    debt_equity_max = vl.get("debt_equity_max")
    top_n = vl.get("top_n", 50)

    tickers = [t["ticker"] for t in _load_tickers()]
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")

    VALUE_SCAN_STATUS.update(running=True, started_at=now_iso, progress=0,
                              total=len(tickers), current_ticker="", finished_at=None,
                              candidates_found=0)

    try:
        candidates: dict[str, dict] = {}
        errors = 0
        for i, ticker in enumerate(tickers):
            VALUE_SCAN_STATUS["progress"] = i + 1
            VALUE_SCAN_STATUS["current_ticker"] = ticker
            try:
                raw = (_fh_get("/stock/metric", {"symbol": ticker, "metric": "all"})
                       .get("metric") or {})
            except Exception as e:
                errors += 1
                log.debug("%s value-metric: %s", ticker, e)
                continue

            mc = raw.get("marketCapitalization")
            if mc is None:
                continue
            mc_usd = mc * 1_000_000
            if mc_usd < mc_min or (mc_max is not None and mc_usd > mc_max):
                continue

            m = _extract_metrics(raw)
            if sum(1 for k in _CORE_FIELDS if m.get(k) is not None) < _MIN_CORE_FIELDS:
                continue
            if pe_max is not None and m["pe"] is not None and m["pe"] > pe_max:
                continue
            if pb_max is not None and m["pb"] is not None and m["pb"] > pb_max:
                continue
            if roe_min is not None and m["roe"] is not None and m["roe"] < roe_min:
                continue
            if debt_equity_max is not None and m["debt_equity"] is not None \
                    and m["debt_equity"] > debt_equity_max:
                continue
            if _distress_veto(m):
                continue

            m["market_cap"] = int(mc_usd)
            candidates[ticker] = m

        VALUE_SCAN_STATUS["candidates_found"] = len(candidates)
        log.info("Value-Scan: %d Kandidaten nach Filter, %d Finnhub-Fehler",
                  len(candidates), errors)
        if not candidates:
            return

        # Rang-basiertes Scoring statt additiver Bonuspunkte (siehe Modul-Docstring)
        value_ranks: dict[str, list] = {t: [] for t in candidates}
        quality_ranks: dict[str, list] = {t: [] for t in candidates}
        for field, higher_is_better in _VALUE_FIELDS:
            vals = {t: m[field] for t, m in candidates.items() if m.get(field) is not None}
            for t, pct in _percentile_ranks(vals, higher_is_better).items():
                value_ranks[t].append(pct)
        for field, higher_is_better in _QUALITY_FIELDS:
            vals = {t: m[field] for t, m in candidates.items() if m.get(field) is not None}
            for t, pct in _percentile_ranks(vals, higher_is_better).items():
                quality_ranks[t].append(pct)

        scored = []
        for ticker, m in candidates.items():
            if not value_ranks[ticker] or not quality_ranks[ticker]:
                continue  # keine einzige Kennzahl in einer der beiden Kategorien verfuegbar
            value_score = sum(value_ranks[ticker]) / len(value_ranks[ticker])
            quality_score = sum(quality_ranks[ticker]) / len(quality_ranks[ticker])
            combined = round(value_score * 0.5 + quality_score * 0.5, 2)
            f_hits, f_available = _f_score_reduced(m)
            scored.append((ticker, combined, m, value_score, quality_score, f_hits, f_available))

        scored.sort(key=lambda x: x[1], reverse=True)
        hits = 0
        for ticker, combined, m, value_score, quality_score, f_hits, f_available in scored[:top_n]:
            details = {
                **m,
                "value_score": round(value_score, 2),
                "quality_score": round(quality_score, 2),
                "f_score_reduced": f_hits,
                "f_score_n_available": f_available,
                "missing_fields": [k for k in _CORE_FIELDS if m.get(k) is None],
            }
            insert_signal(ticker, "value", now_iso, combined, details)
            hits += 1

        log.info("Value-Scan fertig: %d Signale (Top %d von %d Kandidaten)",
                  hits, top_n, len(candidates))
    finally:
        VALUE_SCAN_STATUS.update(
            running=False,
            finished_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
