# Stock Sentiment Scanner

**Live:** `https://umbenennen.duckdns.org/sentiment/`
**Server:** `/opt/sentiment-scanner/` (User: `webhook`, Port: 5005)
**Repo:** `https://github.com/sEppofaz/Stock-Sentiment-Scanner`
**Lokal:** `~/Dropbox/Apps/Claude/Stock Sentiment Scanner/`

---

## Deployment

```bash
# Lokal committen + pushen
git -C ~/Library/CloudStorage/Dropbox/Apps/Claude/Stock\ Sentiment\ Scanner add <datei>
git -C ~/Library/CloudStorage/Dropbox/Apps/Claude/Stock\ Sentiment\ Scanner commit -m "..."
git -C ~/Library/CloudStorage/Dropbox/Apps/Claude/Stock\ Sentiment\ Scanner push

# Auf Server ziehen + neustarten
ssh root@89.167.104.145 "git -C /opt/sentiment-scanner pull && systemctl restart sentiment-scanner"
```

## Service-Befehle

```bash
systemctl status sentiment-scanner
systemctl restart sentiment-scanner
journalctl -u sentiment-scanner -f
```

## Erster Setup (einmalig)

```bash
ssh root@89.167.104.145
git clone https://github.com/sEppofaz/Stock-Sentiment-Scanner /opt/sentiment-scanner
cd /opt/sentiment-scanner
python3 -m venv venv
venv/bin/pip install -r requirements.txt
python3 fetch_tickers.py          # tickers.csv laden (einmalig, quartalsweise wiederholen)
cp sentiment-scanner.service /etc/systemd/system/
chown -R webhook:webhook /opt/sentiment-scanner
systemctl daemon-reload
systemctl enable --now sentiment-scanner
```

## Secrets (in /etc/pka/secrets.env)

- `FINNHUB_API_KEY` – Finnhub Free API Key
- `TOKEN` – Telegram Bot Token (bestehender Hetzner-Bot)
- `CHAT_ID` – Telegram Chat-ID
- `CLAUDE_API_KEY` – Claude API Key (für Sentiment-Anreicherung der Kandidaten)

## nginx-Location

Eingetragen in `/etc/nginx/sites-enabled/rename-webhook` unter `umbenennen.duckdns.org`:
```nginx
location /sentiment/ {
    proxy_pass http://127.0.0.1:5005/;
    proxy_set_header Host $host;
    add_header Cache-Control "no-store";
}
```

## Dateistruktur

```
/opt/sentiment-scanner/
├── venv/               # eigenes venv
├── app.py              # Flask + APScheduler + Icon-Serving + /api/costs
├── scanner.py          # Finnhub-Calls, Filter, Claude-Sentiment, Score, Telegram, Kosten
├── config.json         # editierbar per PWA (keine Credentials!)
├── tickers.csv         # Russell 2000 Ticker (gitignored, quartalsweise neu laden)
├── results.json        # letztes Scan-Ergebnis (gitignored)
├── claude_costs.json   # kumulative Claude API Kosten (gitignored, wird automatisch angelegt)
├── portfolio.json      # persönliche Portfolio-Einträge (gitignored – enthält Kaufpreise!)
├── scan.log            # Protokoll (gitignored)
├── icons/              # cairosvg-generierte PNGs (gitignored)
├── requirements.txt
├── fetch_tickers.py    # Finnhub /stock/symbol?exchange=US → tickers.csv (quartalsweise)
└── pwa/
    ├── index.html      # 4 Tabs: Dashboard, Portfolio, Einstellungen, Kosten
    ├── manifest.json
    └── sw.js
```

## Architektur

- **Stufe 1 (API):** Alle ~4700 Ticker → `/company-news` (7d, Keyword-NLP) → Buzz + Bullish + News-Volumen filtern
- **Stufe 1b (Claude):** Kandidaten (~50–150) → Claude Haiku 4.5 Batch-Sentiment (10 Ticker/Call) → ersetzt Keyword-Scores
- **Stufe 2 (API):** Kandidaten → `/stock/metric` → MarketCap-Filter
- **Score:** 45% Bullish + 30% Buzz (normiert) + 25% NLP-Score + opt. KGV-Bonus
- **Top 50** nach Score → results.json (atomar via tempfile+rename)
- **Telegram:** Top 5 per HTML-formatierter Nachricht + Alert bei neuem €1-Kostenschwellenwert
- **Kosten:** claude_costs.json (kumulativ, pro Scan) + `/api/costs` Endpoint + Kosten-Tab in PWA

## KI-Toggle (ki_enabled)

- `ki_enabled` in `config.json` (Standard: `false`) steuert ob Claude-Anreicherung läuft.
- Toggle im Einstellungen-Tab der PWA → „KI-Analyse (Claude) aktiv".
- Ohne KI: nur Keyword-NLP, 0 € Claude-Kosten. Mit KI: ~0,18 € pro Scan (Haiku 4.5).
- `config.json` ist gitignored → nach `git pull` auf Server nicht überschrieben.

## Phase-Tracking im Scan

`SCAN_STATUS["phase"]` wechselt zwischen `"stufe1"` → `"claude"` → `"stufe2"`.
Frontend zeigt 99% wenn phase = claude oder stufe2 (nicht irreführende 100%).
Zeitschätzung (Min verbleibend) nur während stufe1 wenn progress > 50 Ticker.

## Pitfalls

- **`/news-sentiment` ist KEIN Free-Tier-Endpoint** → gibt 403 zurück → stattdessen `/company-news` verwenden
- **Sentiment-Quelle:** `/company-news` (7d, Headline + Summary) + Keyword-Scoring (BULLISH_WORDS / BEARISH_WORDS in scanner.py)
- **Buzz-Definition:** `buzz = Artikelanzahl / 3.0` (3 Artikel/Woche = 1,0 = "normal") – kein Finnhub-Jahresdurchschnitt mehr
- **Scan-Dauer:** ~90 Min für 4.723 Ticker (55 Calls/min gedrosselt) – nicht 30 Min
- **Scan-Abbruch:** `POST /api/scan/abort` → setzt `SCAN_STATUS["abort"] = True` → Schleife bricht beim nächsten Tick ab
- `marketCapitalization` in Finnhub ist in **Millionen USD** (×1.000.000 für Filtervergleich)
- cairosvg: NIEMALS `write_to=str(path)` → schlägt unter gunicorn fehl → `.write_bytes(data)` verwenden
- Icons-Ordner muss `webhook:webhook` gehören: `chown webhook:webhook /opt/sentiment-scanner/icons`
- tickers.csv ist gitignored → nach `git pull` auf Server separat laden!
- **tickers.csv Quelle:** iShares CSV (blockiert) und Finnhub Index-Endpoints (403 Free Tier) funktionieren nicht → stattdessen Finnhub `/stock/symbol?exchange=US` mit `mic in {XNYS, XNAS}` + `type == "Common Stock"` → 4723 Ticker
- **portfolio.json ist gitignored** (enthält persönliche Kaufpreise und Stückzahlen – nicht ins Repo!)
- **SCAN_STATUS Dict:** thread-sicher via Python GIL für einfache Dict-Reads/Writes – kein Lock nötig
- **`config.json` ist gitignored** – wird NICHT durch `git pull` überschrieben. `config.default.json` im Repo ist die Vorlage; `_load_cfg()` kopiert sie automatisch nach `config.json` wenn die Datei fehlt (Erstinstallation).
- **Claude nur bei gesetztem Key:** `_claude_enrich_batch` wird nur aufgerufen wenn `CLAUDE_API_KEY` in env – ohne Key läuft Keyword-NLP weiter (graceful fallback). Variablenname ist `CLAUDE_API_KEY` (nicht `ANTHROPIC_API_KEY`!)
- **Portfolio-Scan-Frequenz:** Alle 15 Min Mo–Fr 14:00–21:45 UTC (APScheduler), aber nur ausgeführt wenn `_market_open()` True ist (14:30–21:00 UTC = NYSE-Börsenzeiten). Guard: `870 <= h*60+min <= 1260`
- **`scan_enabled` in config.json:** Pausiert Vollscan + Portfolio-Scan (beide Jobs bleiben registriert, prüfen das Flag beim Start). Toggle im Einstellungen-Tab der PWA.
- **`ki_enabled` in config.json:** Steuert Claude-Anreicherung (Standard: false). Toggle im Einstellungen-Tab. Ohne dieses Flag → nur Keyword-NLP.
- **Wochenend-Guard:** `POST /api/scan` gibt 409 zurück an Sa/So (UTC weekday ≥ 5). Frontend zeigt Alert statt API-Call.
- **Portfolio-Scan manuell:** `POST /api/portfolio/scan` – Endpoint für den „Jetzt aktualisieren"-Button im Portfolio-Tab.
- **`_news_texts` ist intern:** Wird in `_fetch_sentiment()` befüllt und vor `_write_results()` aus allen Dicts entfernt – nie in results.json gespeichert
- **Claude Batch-Regex:** Sucht `[...]` im Response-Text mit `re.DOTALL` – robuster als reines JSON-Parsing bei Präambeln
- **claude_costs.json ist gitignored** – atomares Write via tempfile+rename; wird beim ersten Scan automatisch angelegt
- **Telegram-Alert €1-Schwelle:** Nur beim Überschreiten eines neuen Ganzzahlwerts, nicht bei jedem Scan – `last_threshold_notified` in claude_costs.json verhindert Duplikate
- **Sell-Signal nur bei Stimmungsdrehung:** Signal löst NUR aus wenn sich Stimmung ÄNDERT (z.B. Bullish ≥40→<35), nicht bei dauerhaft negativer Stimmung – 5-Punkte-Buffer verhindert Flackern
- **`source /etc/pka/secrets.env` schlägt fehl** auf Ubuntu (Bash-Inkompatibilität bei manchen Zeilen) → `_load_env()` in fetch_tickers.py liest die Datei direkt (server-seitig, keine Ausgabe)
- **Voller Scan vs. Portfolio-Scan:** Beide prüfen gegenseitig `SCAN_STATUS["running"]` – nie gleichzeitig
- **Finnhub Tageslimit:** Bei Limit-Erschöpfung gibt API HTTP 200 zurück mit `{"error":"..."}` (kein Array) – kein HTTP-Fehler, daher kein Exception → `not isinstance(news, list)` fängt das ab, loggt jetzt WARNING mit Response-Preview
- **Early Abort:** Bei 50 aufeinanderfolgenden API-Fehlern bricht der Scan mit ERROR-Log ab (statt 90 Min zu laufen) – `consecutive_errors`-Counter in `run_scan()`
- **Frontend `btn-scan`:** Wird durch `pollScanStatus()` verwaltet (disabled während läuft, enabled wenn fertig) – KEIN setTimeout mehr; bei Netzwerkfehler re-enablet der `catch`-Block sofort

## Frühsignal-Layer (Phase A live seit 2026-07-06, Phasen B/C offen)

- **`EARLY_SIGNALS_UMSETZUNG.md`** = verbindliche Implementierungs-Spec (Phasen A–C), hat Vorrang vor `EARLY_SIGNALS.md` (Konzept/Begründungen)
- Kernentscheidungen: yfinance statt Finnhub-Candles (403 Free Tier), APScheduler statt Cron, SQLite `signals.db` (WAL, gitignored), Feature-Flag `early_signals.enabled` in config.json
- Layer 3 (Buzz-Historie) wird als Hook im bestehenden Vollscan mitgeschrieben – keine neuen API-Calls
- **Phase A live:** `signals_db.py` + `layer1_edgar.py` (EDGAR-Job alle 15 Min, 6–22 Uhr ET Mo–Fr) + buzz_history-Hook in `run_scan()`
- **Pitfall EDGAR-Feed:** `type=4` matcht per Präfix auch 424B*/425 → `_feed_entries()` filtert auf Atom `category term == "4"` (keine /A-Amendments)
- **Pitfall Serverzeit:** Server läuft auf **Europe/Berlin (CEST/CET), NICHT UTC!** APScheduler-Jobs ohne explizite `timezone` laufen in Berlin-Zeit – `scan_times_utc` ist daher faktisch Berlin-Zeit (Altbestand). Neue Jobs immer mit explizitem `timezone`-Parameter anlegen (EDGAR-Job: `America/New_York`)
- `_day_counts` (wie `_news_texts`) nie persistieren – wird vor results.json/portfolio.json gestrippt

## tickers.csv erneuern (quartalsweise)

```bash
ssh root@89.167.104.145
cd /opt/sentiment-scanner
venv/bin/python3 fetch_tickers.py
systemctl restart sentiment-scanner
```

## SW-Cache

Name: `sentiment-v1` – bei Änderungen an manifest.json oder sw.js selbst hochzählen.
