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
- **SCAN_STATUS Dict:** einzelne Dict-Reads/Writes sind dank GIL atomar, aber Check-then-Act-Sequenzen (z.B. "wenn nicht running → starten") sind es NICHT – dafür ist der `running`-Guard direkt in `run_scan()`/`run_portfolio_scan()` eingebaut (nicht nur beim Aufrufer), siehe Fable-5-Review 2026-07-07
- **`config.json` ist gitignored** – wird NICHT durch `git pull` überschrieben. `config.default.json` im Repo ist die Vorlage; `_load_cfg()` kopiert sie automatisch nach `config.json` wenn die Datei fehlt (Erstinstallation).
- **Claude nur bei gesetztem Key:** `_claude_enrich_batch` wird nur aufgerufen wenn `CLAUDE_API_KEY` in env – ohne Key läuft Keyword-NLP weiter (graceful fallback). Variablenname ist `CLAUDE_API_KEY` (nicht `ANTHROPIC_API_KEY`!)
- **Portfolio-Scan-Frequenz:** Alle 15 Min Mo–Fr 9:00–16:45 **America/New_York** (APScheduler, DST-sicher via `zoneinfo`), `_market_open()`-Guard grenzt auf die echte Handelszeit 9:30–16:00 ET ein. **Vorherige Version war falsch:** feste UTC-Grenzen (14:30–21:00 UTC) stimmten nur im Winter (EST) – im Sommer (EDT, z.B. Juli) fehlte dadurch die erste Handelsstunde. Behoben 2026-07-07 (Fable-5-Review, M2).
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

## Frühsignal-Layer (Phase A+B+C komplett live seit 2026-07-07, inkl. PWA-Tab)

- **`EARLY_SIGNALS_UMSETZUNG.md`** = verbindliche Implementierungs-Spec (Phasen A–C), hat Vorrang vor `EARLY_SIGNALS.md` (Konzept/Begründungen)
- Kernentscheidungen: yfinance statt Finnhub-Candles (403 Free Tier), APScheduler statt Cron, SQLite `signals.db` (WAL, gitignored), Feature-Flag `early_signals.enabled` in config.json
- Layer 3 (Buzz-Historie) wird als Hook im bestehenden Vollscan mitgeschrieben – keine neuen API-Calls
- **Phase A live:** `signals_db.py` + `layer1_edgar.py` (EDGAR-Job alle 15 Min, 6–22 Uhr ET Mo–Fr) + buzz_history-Hook in `run_scan()`
- **Phase B live:** `layer2_volume.py` (Volumen-z-Score via yfinance, 17:15 ET) + `layer3_buzz.py` (Buzz-Beschleunigung aus buzz_history, 17:25 ET, keine API-Calls)
- **Pitfall EDGAR-Feed:** `type=4` matcht per Präfix auch 424B*/425 → `_feed_entries()` filtert auf Atom `category term == "4"` (keine /A-Amendments)
- **Pitfall Serverzeit:** Server-Systemzeit ist **Europe/Berlin, NICHT UTC!** APScheduler-Jobs ohne explizite `timezone` liefen in Berlin-Zeit statt UTC (Portfolio-Scan endete real 19:45 statt 21:00 UTC) → **behoben 2026-07-06**: `scan_*`- und `portfolio_scan`-Jobs haben jetzt `timezone="UTC"`. Neue Jobs immer mit explizitem `timezone`-Parameter anlegen (Frühsignal-Jobs: `America/New_York`)
- **Phase C (Backend) live:** `layer4_scoring.py` (Kombinations-Scoring + Telegram-Alert, 17:35 ET) + `forward_tracker.py` (füllt forward_returns, 17:45 ET) + Endpoint `/sentiment/api/early-signals` (signals/alerts/stats für den künftigen PWA-Tab)
- **PWA-Tab „Frühsignale" live (2026-07-07):** 5. Tab mit Alert-Liste, Signal-Feed (letzte 100), Trefferquoten-Statistik je Horizont (1/5/20 Handelstage) aus `/api/early-signals`. `alerts.signal_ids` ist ein JSON-**String** (nicht dekodiert von SQLite) → `JSON.parse()` nötig vor Nutzung im Frontend.
- **Pitfall „Aktuell:"-Werte im Info-Sheet (2026-07-07):** Die `Aktuell: X`-Zeilen im „Einstellungen erklärt"-Abschnitt sind statischer Text in `index.html`, nicht aus `config.json` generiert. Bei jeder Änderung an Scan-Zeiten, Top-N, Filterwerten etc. driften sie stillschweigend auseinander (gefunden: Scan-Zeiten + Top N waren veraltet). Nach Config-Änderungen über die PWA-Einstellungen diese Texte manuell nachziehen, oder künftig serverseitig aus der Config rendern statt hardcoden.
- **Kritischer Bugfix (2026-07-07):** `saveConfig()` im Frontend baute das Config-Objekt bei jedem Speichern aus den Formularfeldern neu zusammen – der `early_signals`-Block war darin nicht enthalten. Da `POST /api/config` die komplette `config.json` durch das gesendete JSON ersetzt, hätte jedes normale Speichern in den Einstellungen den Frühsignal-Layer stillschweigend deaktiviert. Fix: `loadConfig()` merkt sich das volle geladene Objekt in `_cfg`, `saveConfig()` spreadet `{..._cfg, ...}` statt neu zu bauen. **Regel:** Jedes zusätzliche Top-Level-Config-Feld (auch künftige) muss beim Bauen von `saveConfig()`-Payloads erhalten bleiben – am saubersten über Spread von der zuletzt geladenen Config, nie durch Neuaufbau aus einzelnen Formularfeldern.

## Layer 5: 13D/13G-Großaktionärsmeldungen (2026-07-08)

Josef-Frage "welche Möglichkeiten können neben Volumen und Insider noch auftreten?" → 13D/13G gewählt, weil dieselbe EDGAR-Infrastruktur wiederverwendbar ist.

- **`layer5_ownership.py`**, neuer Scheduler-Job `ownership_scan` (15 Min, 6–22 ET, wie `edgar_scan`). Neuer Signal-Typ `large_holder`.
- **Schedule 13D** = aktiver/aktivistischer Investor (kann Einfluss/Kontrolle anstreben), muss binnen 10 Tagen nach Überschreiten der 5%-Schwelle gemeldet werden → Gewichtung 3.0, löst als Einzelsignal IMMER einen Instant-Alert aus.
- **Schedule 13G** = passiver Investor (Indexfonds etc.), häufiger/schwächer → Gewichtung 1.5, Instant-Alert erst ab `single_large_holder_13g_min_pct` (Default 7.0%, config-Key unter `early_signals`).
- **Beide Formulare sind seit einer SEC-Modernisierung strukturiertes XML** (`primary_doc.xml`, analog Form 4) – verifiziert 2026-07-08 gegen echte Filings (Marchex-13D, Accuray-13G). **13D und 13G nutzen aber UNTERSCHIEDLICHE Schemas/Tag-Namen** für denselben Sachverhalt: `issuerCIK` (13D) vs `issuerCik` (13G), `percentOfClass` vs `classPercent`, `aggregateAmountOwned` vs `reportingPersonBeneficiallyOwnedAggregateNumberOfShares`. Lösung: `_local()`/`_local_first()` suchen per lokalem Tag-Namen (namespace-unabhängig) statt fixer XPath.
- **Pitfall ElementTree `or`-Verkettung (live gefunden beim ersten Test):** `element_a or element_b` prüft bei ElementTree-Elementen NICHT auf `None`, sondern nutzt `len(element) > 0` als Truthy-Wert – ein reines Text-Blatt-Element (z.B. `<issuerCIK>1234</issuerCIK>` ohne Kind-Elemente) ist dadurch **falsy**, obwohl es gültig und nicht-`None` ist. `_local_first(root, "issuerCIK", "issuerCik")` mit expliziten `is not None`-Checks statt `or`-Verkettung verwenden. **Regel:** Bei ElementTree-Elementen NIE `el_a or el_b`, immer `if el_a is not None: ... else: ...` oder eine Helper-Funktion mit explizitem None-Check.
- **CIK→Ticker-Auflösung** über SECs offizielle `https://www.sec.gov/files/company_tickers.json` (kostenlos, kein Auth, ~1MB), im Speicher gecacht (1x/Tag neu geladen). Notwendig weil 13D/13G (anders als Form 4) keinen Ticker direkt im XML enthalten, nur die Issuer-CIK.
- **`type=`-Filter im EDGAR-getcurrent-Feed:** `SC 13D`/`SC 13G` (nicht `SCHEDULE 13D` – das ist nur die menschenlesbare Form im Daily-Index/Submission-Header). Category-Term im Atom-Feed exakt matchen (`term == "SC 13D"`), sonst matchen auch `.../A`-Amendments (gleicher Pitfall wie Form 4 `type=4`).
- **v1-Scope bewusst einfach gehalten:** Nur Original-Filings (keine `/A`-Amendments), nur der erste Reporting-Person-Name bei gemeinsamen Meldern (z.B. Fondsfamilie) – für ein Frühsignal ausreichend, keine vollständige rechtliche Offenlegung.
- **Filter-Chips im Signal-Feed:** 4 Typen togglebar (`ES_FILTER_TYPES` in `pwa/index.html`), rein clientseitig auf bereits geladenen Daten – kein neuer API-Call nötig.

## Instant-Alerts + Auto-Watch (2026-07-08)

Josef-Feedback: Der News-Sentiment-Scanner ist strukturell zu spät (Presse berichtet erst nach der Bewegung); der bisherige Frühsignal-Kombi-Alert (≥2 Signal-Typen, nur 1x täglich um 17:35 ET) war ihm ebenfalls zu träge.

- **`check_instant_alerts()`** in `layer4_scoring.py`, neuer Scheduler-Job `es_instant` (alle 15 Min, 6–22 Uhr ET, gleiche Zeitspanne wie EDGAR) – prüft Signale der letzten 20 Minuten auf Einzelsignal-Stärke: Insider-Cluster ODER Kauf ≥ `single_insider_min_usd` (Default 100.000 $), Volumen-z-Score ≥ `single_volume_z_min` (Default 6.0), Buzz-Beschleunigung ≥ `single_buzz_accel_min` (Default 3.0 = 300%). Löst sofort aus, ohne auf ein zweites Signal zu warten.
- **`_create_alert()`-Helper** in `layer4_scoring.py` extrahiert – gemeinsamer Pfad für `run_scoring()` (Kombi, täglich) und `check_instant_alerts()` (Einzelsignal, alle 15 Min). Beide teilen sich denselben 7-Tage-Cooldown pro Ticker (verhindert Doppel-Alert wenn beide Pfade denselben Ticker treffen).
- **Auto-Watch:** `_auto_watch()` fügt jeden Alert-Ticker automatisch mit `shares:1, buy_price:<Kurs bei Alert>, watch:true` ins Portfolio ein (kein echter Kauf) – Portfolio-Scan/P&L-Logik läuft unverändert weiter, PWA zeigt bei `watch:true` „Rendite seit Signal" statt Einstand/Positionswert. Abschaltbar via `early_signals.auto_watch` (Default `true`).
- **Dashboard-Box:** `pwa/index.html` zeigt die letzten 5 Alerts aus `/api/early-signals` oben im Dashboard-Tab, unabhängig vom News-Sentiment-Filter (der Frühsignal-Ticker sonst gar nicht zeigen würde, da noch keine Presseartikel existieren).
- **Pitfall Score-Skala:** `total_score` bei Instant-Alerts ist der rohe Signalwert (z.B. z-Score 7.2 oder rel_accel 3.5), NICHT die Gewichts-Skala (1–5) aus `run_scoring()`. In der UI als „Score" gerundet angezeigt – bewusst so belassen (informativer als eine künstliche Normierung), aber beim Lesen der Alert-Liste nicht direkt mit Kombi-Alert-Scores vergleichen.
- **`single_*`-Config-Keys müssen auf dem Server manuell nachgezogen werden** falls `config.json` vor 2026-07-08 angelegt wurde (gitignored, kein automatischer Merge durch `git pull`) – Code nutzt `.get(key, default)`, funktioniert auch ohne die Keys, aber dann sind sie nicht sichtbar/editierbar in `GET /api/config`.
- **Pitfall Signal-Feed-Lesbarkeit (behoben 2026-07-08):** `.es-signal-type` hatte `white-space:nowrap;overflow:hidden;text-overflow:ellipsis` → lange Detail-Zeilen (Insider-Name, Beträge) wurden abgeschnitten, und Volumen-Anomalie zeigte nur den abstrakten z-Score statt der realen Volumenzahl. Jetzt zweizeiliges Layout (`.es-signal-top` + `.es-signal-detail`, normaler Zeilenumbruch) + `esFormatDetail()` zeigt vollständige Werte (Volumen + Vielfaches des 20-Tage-Ø, Aktienanzahl bei Insider-Käufen, Artikelzahlen bei Buzz-Accel). **Regel für neue Kompakt-Darstellungen:** nie `nowrap+ellipsis` auf Feldern mit potenziell langem/wichtigem Inhalt (Namen, Beträge) – lieber zweizeilig mit normalem Umbruch.

## Fable-5-Review 2026-07-07 – Fixes

Vollständiger unabhängiger Code-Review (4 kritisch, 9 mittel, 10 gering). K4 (keine API-Authentifizierung) bewusst zurückgestellt – eigene Architekturentscheidung, noch offen. Alle anderen Findings gefixt:

- **K1 Portfolio-Datenverlust:** Vollscan lud `portfolio` einmal zu Scan-Beginn (90 Min Laufzeit) und überschrieb am Ende mit der veralteten Kopie – während des Scans hinzugefügte/gelöschte Aktien oder gesetzte Sell-Signale gingen verloren. Fix: `_update_portfolio_quotes(_load_portfolio())` lädt jetzt unmittelbar vor dem Schreiben frisch.
- **K2 Race Condition:** `run_portfolio_scan()` hatte keinen eigenen `running`-Guard (nur die Aufrufer hatten einen, aber nicht alle – `api_portfolio_add`s Hintergrundthread nicht). Fix: Guard jetzt zentral in `run_portfolio_scan()` selbst.
- **K3 Hängender Scan-Status:** Warf `run_scan()`/`run_portfolio_scan()` eine Exception, blieb `SCAN_STATUS["running"]` für immer `True` → alle künftigen Scans wurden bis zum Service-Neustart stillschweigend übersprungen. Fix: Beide Funktionen sind jetzt dünne Wrapper mit `try/finally` um eine `_inner()`-Funktion.
- **M1 Abort-Leck:** `abort`-Flag wurde nicht bei Scan-Start zurückgesetzt und in `run_portfolio_scan()` nie geprüft/resettet – ein während eines Portfolio-Scans gedrückter Abbruch-Button ließ den NÄCHSTEN Vollscan sofort mit 0 Ergebnissen abbrechen. Fix: `abort: False` in beiden Start-Updates, Abort-Check auch in der Portfolio-Scan-Schleife.
- **M2 DST-Bug:** siehe Portfolio-Scan-Frequenz oben.
- **M3 Abort überschreibt gute Daten:** Bei Scan-Abbruch (manuell oder Early-Abort-Schwelle) wurden `results.json` und Telegram-Top5 trotzdem mit dem unvollständigen Ergebnis geschrieben. Fix: Bei `aborted=True` werden Claude-Anreicherung/Stufe2/Schreiben/Telegram übersprungen, letztes gutes Ergebnis bleibt erhalten.
- **M4 `_news_texts`/`_day_counts`-Leck:** Im Pinned-Portfolio-Pfad konnte ein frischer `_fetch_sentiment()`-Call (Ticker nicht in `all_scanned`) ungestrippte interne Felder nach `results.json` durchreichen. Fix: Strip direkt vor `top_n.append(base)`.
- **M5 EDGAR-Signal-Kollision:** `signal_ts` wurde einmal pro Lauf statt pro Filing gesetzt → zwei Insider-Käufe desselben Tickers im selben 15-Min-Lauf kollidierten am `UNIQUE(ticker, signal_type, signal_ts)`, das zweite wurde von `INSERT OR IGNORE` stumm verworfen. Fix: `signal_ts` aus dem Atom-`<updated>`-Feld je Filing (normalisiert auf UTC via `_normalize_ts()`).
- **M7 Config-Validierung:** `POST /api/config` schrieb beliebiges JSON ungeprüft; eine kaputte `scan_times_utc`-Zeile hätte `_reschedule()` bei jedem künftigen Service-Start crashen lassen (Restart-Loop). Fix: `_validate_cfg()` vor dem Schreiben + defensives `try/except` pro Scan-Zeit in `_reschedule()`.
- **M8 Retention/Cleanup:** `buzz_history`/`edgar_seen` wuchsen unbegrenzt, `claude_costs.json["scans"]` ebenso, `scan.log` ohne Rotation. Fix: täglicher Cleanup-Job 03:00 UTC (`signals_db.cleanup_old_data()`: buzz_history >60 Tage, edgar_seen >30 Tage), `scans`-Liste auf letzte 200 gekappt, `RotatingFileHandler` (5 MB × 3).
- **M9 Throttle nicht threadsicher:** `_call_times`-Rebind in `_throttle()` war nicht atomar – mehrere Scheduler-Jobs (Vollscan, EDGAR, Layer2, Layer4) teilen sich das Finnhub-Budget. Fix: `threading.Lock` um die gesamte Funktion.
- **G1 SQLite-Connections:** `with get_conn() as conn:` committete, schloss aber nie (`sqlite3.Connection.__exit__` schließt nicht). Fix: `get_conn()` ist jetzt ein `@contextmanager`, der committet/rollt zurück UND schließt – alle Aufrufstellen unverändert lauffähig.
- **G2 Zeitstempel-Format:** `signal_ts`/`alert_ts` sind ISO mit `T`+Offset, verglichen gegen `datetime('now', ...)` (Format mit Leerzeichen) – lexikografisch bis zu 24h ungenau. Fix: `strftime('%Y-%m-%dT%H:%M:%S', 'now', ...)` in allen betroffenen Queries (layer1_edgar, layer4_scoring).
- **G3 Telegram-Fehler verschluckt:** `_tg_post()` prüfte den Response-Status nicht; ungeschätzte externe Namen (Company-Name, Insider-Owner) konnten Telegram-HTML brechen → Alert ging still verloren. Fix: Status-Check + Log-Warning, `html.escape()` auf allen extern-kontrollierten Textfeldern in Telegram-Nachrichten.
- **G4 Buzz-Median verzerrt:** `_news_flat()` bildete den Median nur über Tage MIT Artikeln (buzz_history speichert keine 0-Zeilen) → Median systematisch zu hoch, Filter zu permissiv. Fix: Median jetzt über alle 30 Kalendertage (fehlende Tage = 0).
- **G7 forward_tracker:** Bis zu 3 identische `yf.download()`-Calls pro Alert (einer je Horizont) + tote `forward_returns`-Zeilen bei `price_at_alert IS NULL` (können nie gefüllt werden). Fix: nach `(ticker, alert_id)` gruppiert (ein Download pro Alert), `layer4_scoring.py` legt keine Zeilen mehr an wenn kein Preis geholt werden konnte.
- **G8 `api_portfolio_add`:** `float()` auf Garbage-Input warf 500 statt 400. Fix: `try/except` mit sauberer 400-Antwort.
- **G9 Gunicorn-Falle (neu dokumentiert):** `scheduler.start()` + `init_db()` laufen auf Modulebene in `app.py`. Aktuell unkritisch (`python3 app.py` direkt im systemd-Unit, kein Multi-Worker). **Bei künftigem Wechsel auf gunicorn mit >1 Worker würden alle Scheduler-Jobs mehrfach laufen** (doppelte Scans, doppelte Telegram-Alerts) – vorher WSGI-Server-Wechsel hier eintragen und Guard einbauen (z.B. nur in Worker 0 starten).
- **G10 Server-Lokalzeit bei `_day_counts` (neu dokumentiert):** `_fetch_sentiment()` nutzt `date.fromtimestamp(...)` (scanner.py) → Server-Lokalzeit (Europe/Berlin), nicht UTC oder US-Handelstag. US-Abendnews (nach 18 Uhr ET) rutschen auf den Berliner Folgetag in `buzz_history`. In sich konsistent (Layer 3 rechnet mit derselben Zeitbasis), aber „Handelstag"-Semantik ist gegenüber ET verschoben – bewusst nicht geändert (Breaking Change für bestehende buzz_history-Daten), nur dokumentiert.
- **Pitfall yfinance:** `yf.download(tickers=[...], group_by="ticker")` liefert bei Listen- UND bei Einzel-String-Übergabe IMMER MultiIndex-Spalten. Bei Liste: `data[sym]["Volume"]` (auch bei 1 Ticker im Chunk). Bei Einzel-Ticker-String (kein `group_by`, wie im Forward-Tracker): `hist["Close"]` ist ein **DataFrame**, nicht Series → `hist["Close"][ticker]` nötig, sonst crasht `float(...)` für jeden Ticker (verifiziert 2026-07-06, Spec hatte hier einen Fehler)
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
