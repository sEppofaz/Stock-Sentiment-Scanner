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
├── app.py              # Flask + APScheduler + Icon-Serving
├── scanner.py          # Finnhub-Calls, Filter, Score, Telegram
├── config.json         # editierbar per PWA (keine Credentials!)
├── tickers.csv         # Russell 2000 Ticker (gitignored, quartalsweise neu laden)
├── results.json        # letztes Scan-Ergebnis (gitignored)
├── portfolio.json      # persönliche Portfolio-Einträge (gitignored – enthält Kaufpreise!)
├── scan.log            # Protokoll (gitignored)
├── icons/              # cairosvg-generierte PNGs (gitignored)
├── requirements.txt
├── fetch_tickers.py    # Finnhub /stock/symbol?exchange=US → tickers.csv (quartalsweise)
└── pwa/
    ├── index.html
    ├── manifest.json
    └── sw.js
```

## Architektur

- **Stufe 1 (API):** Alle ~4700 Ticker → `/company-news` (7d, Keyword-NLP) → Buzz + Bullish + News-Volumen filtern
- **Stufe 2 (API):** Kandidaten → `/stock/metric` → MarketCap-Filter
- **Score:** 45% Bullish + 30% Buzz (normiert) + 25% NLP-Score + opt. KGV-Bonus
- **Top 50** nach Score → results.json (atomar via tempfile+rename)
- **Telegram:** Top 5 per HTML-formatierter Nachricht

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
- **Sell-Signal nur bei Stimmungsdrehung:** Signal löst NUR aus wenn sich Stimmung ÄNDERT (z.B. Bullish ≥40→<35), nicht bei dauerhaft negativer Stimmung – 5-Punkte-Buffer verhindert Flackern
- **`source /etc/pka/secrets.env` schlägt fehl** auf Ubuntu (Bash-Inkompatibilität bei manchen Zeilen) → `_load_env()` in fetch_tickers.py liest die Datei direkt (server-seitig, keine Ausgabe)
- **Voller Scan vs. Portfolio-Scan:** Beide prüfen gegenseitig `SCAN_STATUS["running"]` – nie gleichzeitig
- **Finnhub Tageslimit:** Bei Limit-Erschöpfung gibt API HTTP 200 zurück mit `{"error":"..."}` (kein Array) – kein HTTP-Fehler, daher kein Exception → `not isinstance(news, list)` fängt das ab, loggt jetzt WARNING mit Response-Preview
- **Early Abort:** Bei 50 aufeinanderfolgenden API-Fehlern bricht der Scan mit ERROR-Log ab (statt 90 Min zu laufen) – `consecutive_errors`-Counter in `run_scan()`
- **Frontend `btn-scan`:** Wird durch `pollScanStatus()` verwaltet (disabled während läuft, enabled wenn fertig) – KEIN setTimeout mehr; bei Netzwerkfehler re-enablet der `catch`-Block sofort

## tickers.csv erneuern (quartalsweise)

```bash
ssh root@89.167.104.145
cd /opt/sentiment-scanner
venv/bin/python3 fetch_tickers.py
systemctl restart sentiment-scanner
```

## SW-Cache

Name: `sentiment-v1` – bei Änderungen an manifest.json oder sw.js selbst hochzählen.
