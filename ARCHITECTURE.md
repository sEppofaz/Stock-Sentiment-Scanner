# Stock Sentiment Scanner — Architektur

**Zweck:** Täglich zweimal Small-Cap-Aktien (Russell 2000) auf steigendes News-Sentiment scannen und per Telegram + PWA ausgeben.  
**Stack:** Python 3, Hetzner CX23 (Ubuntu 24.04), bestehender Telegram-Bot, Finnhub Free API  
**Stand:** Architekturentwurf — noch nicht implementiert

---

## Übersicht

```
┌─────────────────────────────────────────────────────────┐
│                    HETZNER CX23                         │
│                                                         │
│  ┌──────────┐    ┌──────────────┐    ┌───────────────┐ │
│  │ Cron /   │───▶│  scanner.py  │───▶│  results.json │ │
│  │ APScheduler│  │  (Kern-Logik)│    │  + scan.log   │ │
│  └──────────┘    └──────┬───────┘    └───────┬───────┘ │
│                         │                    │         │
│                  ┌──────▼───────┐    ┌───────▼───────┐ │
│                  │ Finnhub API  │    │  Telegram Bot │ │
│                  │ (extern)     │    │  (Push-Alert) │ │
│                  └──────────────┘    └───────────────┘ │
│                                                         │
│  ┌──────────────────────────────────────────────────┐  │
│  │  PWA (Flask, bestehende Infrastruktur)           │  │
│  │  liest results.json → zeigt Tabelle + Charts     │  │
│  └──────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────┘
```

---

## Komponenten

### 1. Datenbasis: Russell 2000 Ticker-Liste

- **Quelle:** Statische CSV-Datei (~2000 Ticker)  
- **Herkunft:** iShares Russell 2000 ETF Holdings (öffentlich, kostenlos): https://www.ishares.com/us/products/239710/  
- **Format:** `tickers.csv` mit Spalten: `ticker, company_name`  
- **Update:** Manuell quartalsweise, oder automatisch per Download-Skript  
- **Warum Russell 2000:** Definierter Small-Cap-Index (Marktcap ca. $300M–$2B), öffentlich bekannte Zusammensetzung, keine API nötig für das Universum

### 2. scanner.py — Kern-Logik

Ablauf pro Scan-Durchlauf:

```
1. Lade tickers.csv
2. Für jeden Ticker (mit Rate-Limit: max. 55 Calls/Min):
   a. GET /stock/metric  → marketCap, peRatio, pbRatio
   b. GET /news-sentiment → bullishPercent, bearishPercent, buzz
   c. GET /company-news  → letzte 7 Tage Headlines
3. Filter anwenden (siehe Filterlogik)
4. Top-N Ergebnisse nach Score sortieren
5. Schreibe results.json
6. Sende Telegram-Nachricht mit Top-5
7. Schreibe scan.log (Zeitstempel, Anzahl gescannte Ticker, Fehler)
```

**Wichtig:** 2000 Ticker × 2-3 API Calls = ~4000-6000 Calls pro Durchlauf.  
Bei 55 Calls/Min = ca. 70-110 Minuten pro Durchlauf.  
→ **Lösung:** Nur Ticker scannen, die in `watchlist_active.csv` stehen (manuell gepflegte Shortlist, z.B. 100-200 Ticker). Russell 2000 dient als Auswahlpool, nicht als täglicher Scan-Input.

### 3. Filterlogik

```python
# Stufenfilter — alle Bedingungen müssen erfüllt sein (Pflichtfelder)

FILTER = {
    "market_cap_max_usd": 2_000_000_000,   # < $2B (Small Cap)
    "market_cap_min_usd":   50_000_000,    # > $50M (kein Penny Stock)
    "bullish_pct_min":              40,    # mind. 40% bullische News
    "bearish_pct_max":              30,    # max. 30% bearish
    "buzz_trend":               "rising",  # Buzz steigt (Trend-Feld Finnhub)
    "news_min_count":                3,    # mind. 3 News in 7 Tagen
}

# Score-Formel (gewichtet, 0-100):
# KGV fließt optional ein — nur wenn Finnhub den Wert liefert (nicht null)
# Quelle KGV: Finnhub /stock/metric → Feld "peNormalizedAnnual"
# Achtung: Bei Small Caps häufig null oder negativ (kein Gewinn) → nie als Pflichtfilter

def calc_score(ticker_data):
    base_score = (
        0.45 * ticker_data["bullish_pct"] +          # Sentiment-Kern
        0.30 * ticker_data["buzz_score"] +            # Aufmerksamkeit
        0.25 * ticker_data["sentiment_score_norm"]    # NLP-Score
    )

    # KGV-Bonus: optional, nur wenn vorhanden und sinnvoll (0 < KGV < 30)
    pe = ticker_data.get("peNormalizedAnnual")
    if pe is not None and 0 < pe < 30:
        # Niedriges KGV = höherer Bonus (max. +10 Punkte)
        pe_bonus = max(0, (30 - pe) / 30 * 10)
        base_score += pe_bonus

    return round(min(base_score, 100), 2)
```

**Warum KGV nur optional:**  
Laut Finnhub-Dokumentation und bestätigten Praxisberichten sind Fundamentaldaten bei
kleinen, wenig bekannten Unternehmen häufig unvollständig (`null`). Zusätzlich haben
wachstumsstarke Small Caps oft negatives oder sehr hohes KGV — ein harter KGV-Filter
würde genau diese Kandidaten ausschließen. Daher: KGV als Bonus im Score, nie als
Disqualifikationskriterium.

**Hinweis zur "noch nicht eingepreist"-Logik:**  
Es gibt keine zuverlässige API-Metrik dafür. Annäherung: steigendes Sentiment bei noch
niedrigem Buzz-Score (z.B. buzz < 40 aber Trend = rising). Das ist eine Heuristik, kein Beweis.

### 4. Konfiguration — config.json (editierbar in PWA)

```json
{
  "scan_times_utc": ["13:00", "19:30"],
  "market": "US",
  "filter": {
    "market_cap_max_usd": 2000000000,
    "market_cap_min_usd": 50000000,
    "bullish_pct_min": 40,
    "bearish_pct_max": 30,
    "buzz_trend": "rising",
    "news_min_count": 3
  },
  "top_n_results": 10,
  "telegram_chat_id": "DEINE_CHAT_ID",
  "finnhub_api_key": "DEIN_KEY"
}
```

Die PWA liest und schreibt diese Datei direkt. Scan-Zeiten können damit ohne SSH geändert werden.

### 5. Telegram-Output (Beispiel)

```
📊 Stock Sentiment Scan — 14:30 UTC

🟢 TOP 5 Small Caps (Rising Sentiment)

1. ACMR — ACM Research
   Score: 82 | Bullish: 67% | Buzz: ↑ rising
   MarketCap: $420M | 5 News (7d)

2. MGNI — Magnite Inc.
   Score: 76 | Bullish: 58% | Buzz: ↑ rising
   MarketCap: $890M | 3 News (7d)

[... bis Top 5]

ℹ️ 187 Ticker gescannt | Finnhub Free API
⚠️ Kein Investment-Advice. Nur Sentiment-Daten.
```

### 6. PWA (Flask)

Bestehende Flask-Infrastruktur auf Hetzner erweitern:

```
/sentiment/              → Dashboard (Tabelle, sortierbar)
/sentiment/config        → Einstellungen (Scan-Zeiten, Filter)
/sentiment/log           → Scan-Log anzeigen
/sentiment/api/results   → JSON-Endpunkt für Frontend
```

Frontend: Vanilla JS + bestehende PWA-Struktur (kein neues Framework).  
Daten kommen aus `results.json` — kein DB nötig in Phase 1.

### 7. Scheduler

Option A (einfacher): **Cron** auf Hetzner  
```cron
0 13 * * 1-5 /usr/bin/python3 /home/claude/scanner/scanner.py
30 19 * * 1-5 /usr/bin/python3 /home/claude/scanner/scanner.py
```
Nachteil: Scan-Zeiten nicht dynamisch aus config.json lesbar.

Option B (empfohlen): **APScheduler** als Daemon  
```python
# scheduler_daemon.py — läuft permanent, liest config.json
from apscheduler.schedulers.blocking import BlockingScheduler
import json, subprocess

scheduler = BlockingScheduler()

def load_and_reschedule():
    config = json.load(open("config.json"))
    # Jobs neu registrieren bei Änderung
    ...

# Damit sind Scan-Zeiten per PWA änderbar ohne Server-Restart
```

---

## Dateistruktur (Ziel)

```
/home/claude/sentiment_scanner/
├── scanner.py              # Kern-Logik
├── scheduler_daemon.py     # APScheduler
├── config.json             # Konfiguration (editierbar per PWA)
├── tickers.csv             # Russell 2000 Ticker-Pool
├── watchlist_active.csv    # Aktiv zu scannende Ticker (Shortlist)
├── results.json            # Letztes Scan-Ergebnis
├── scan.log                # Protokoll
└── pwa/
    ├── app.py              # Flask-Routen (Integration in bestehenden Flask)
    ├── templates/
    │   ├── dashboard.html
    │   └── config.html
    └── static/
        └── sentiment.js
```

---

## API-Limits & Risiken

| Thema | Detail |
|---|---|
| Finnhub Free: 60 Calls/Min | Bei 200 Ticker × 2 Calls = 400 Calls → ~7 Min/Scan. Passt. |
| Finnhub Free: Small Cap Datenlücken | Bei unbekannten Small Caps können Felder null sein. Muss abgefangen werden. |
| Finnhub ToS | Kommerzielle Nutzung erfordert bezahlten Plan. Für persönliche Nutzung/Research: Free Tier erlaubt. |
| "Noch nicht eingepreist" | Nicht direkt messbar. Buzz-Trend als Annäherung — ist eine Heuristik. |
| Russell 2000 CSV | Muss manuell aktualisiert werden. Quartalsweise rebalancing des Index beachten. |

---

## Phase 1 (MVP) — Reihenfolge für Claude Code

1. `tickers.csv` — Russell 2000 laden (Download-Skript oder manuell)
2. `scanner.py` — Finnhub-Calls + Filterlogik + results.json schreiben
3. Telegram-Integration — bestehenden Bot nutzen
4. `scheduler_daemon.py` — APScheduler mit config.json
5. PWA Dashboard — Flask-Route + HTML-Tabelle
6. PWA Config-Editor — Scan-Zeiten + Filter per UI änderbar

**Nicht in Phase 1:** Historische Sentiment-Trends, Backtesting, DB.

---

## Offene Fragen vor Start

- [ ] Finnhub API Key vorhanden? → https://finnhub.io/register (kostenlos)
- [ ] Welche Telegram Bot Chat-ID soll verwendet werden? (bestehender Bot?)
- [ ] Flask-App läuft bereits auf Port X? (Integration vs. neue App)
- [ ] Shortlist: Startest du mit dem vollen Russell 2000 oder einer manuellen Watchlist?
