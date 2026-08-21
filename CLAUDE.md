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

# Auf Server ziehen + neustarten (chown zwingend nach jedem Pull-als-root, siehe Pitfall unten!)
ssh root@89.167.104.145 "git -C /opt/sentiment-scanner pull && chown -R webhook:webhook /opt/sentiment-scanner && systemctl restart sentiment-scanner"
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

**Login (seit 2026-08-09, nicht in secrets.env):** `/etc/nginx/sentiment.htpasswd` (Passwort, von Josef selbst per `htpasswd -c` gesetzt) + `/opt/sentiment-scanner/.session_key` (autogeneriert beim ersten Start, `chmod 600`, gitignored).

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
├── signals_db.py        # SQLite-Layer (signals.db): Frühsignale + Scan-Snapshot-Historie + Weekly-Reports
├── layer1_edgar.py .. layer5_ownership.py  # Frühsignal-Layer (Insider/Volumen/Buzz/Scoring/13D-13G)
├── layer6_daily_pick.py    # Tages-Konsolidierung: höchstens 1 Kauf-Pick/Tag über beide Systeme
├── layer6_sell_signal.py   # Verkaufssignal aus Frühsignal-Gegensignalen, nur für echte Positionen
├── forward_tracker.py  # füllt forward_returns (Frühsignal-Alerts, 1/5/20 Handelstage)
├── scan_tracker.py     # füllt scan_forward_returns (Sentiment-Scan-Snapshots, 1/5/20 Handelstage)
├── yf_helper.py         # gemeinsamer yfinance-Zugriff für beide Tracker (ADR-010)
├── weekly_analysis.py   # wöchentliche Performance-Analyse (regelbasiert + optionaler KI-Button)
├── config.json         # editierbar per PWA (keine Credentials!)
├── tickers.csv         # Russell 2000 Ticker (gitignored, quartalsweise neu laden)
├── results.json        # letztes Scan-Ergebnis (gitignored)
├── signals.db           # SQLite: signals/alerts/forward_returns + scan_snapshots/scan_forward_returns/weekly_reports (gitignored, WAL)
├── claude_costs.json   # kumulative Claude API Kosten (gitignored, wird automatisch angelegt)
├── portfolio.json      # persönliche Portfolio-Einträge (gitignored – enthält Kaufpreise!)
├── scan.log            # Protokoll (gitignored)
├── icons/              # cairosvg-generierte PNGs (gitignored)
├── requirements.txt
├── fetch_tickers.py    # Finnhub /stock/symbol?exchange=US → tickers.csv (quartalsweise)
└── pwa/
    ├── index.html      # 4 Tabs: Dashboard, Portfolio, Früh, Analyse (Einstellungen + Kosten als Header-Icons, seit 2026-08-09)
    ├── login.html      # Flask-Session-Login (seit 2026-08-09, ADR-015), öffentlich erreichbar
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

## Tages-Kosten-Tracking mit Hard-Kill (2026-07-24)
Zusätzlich zur bestehenden Lifetime-EUR-Schwelle (`_update_claude_costs`, `total_cost_eur`/`scans[]`, unverändert) trackt `costs.py` jetzt parallel ein Tages-Tracking in USD (Session = Kalendertag). Bei 1$/Tag Telegram-Info (läuft weiter), bei 5$/Tag setzt `_claude_enrich_batch()` `SCAN_STATUS["abort"]=True` und bricht die Batch-Schleife selbst ab (bestehender Abort-Skip-Pfad greift danach). `/api/costs` mergt alte+neue Felder. Details: ADR-008, `PKA/BKM/Claude-API-Kosten-Tracking.md`.
- `costs.py`/`_load()`: bei bereits existierender `claude_costs.json` (hier: das alte EUR/scans-Format) immer `dict.update(raw)` auf einen Default-Dict, nie `return raw` direkt – sonst `KeyError` auf `calls`/`daily` (live aufgetreten beim Rollout, siehe Newsletter-CLAUDE.md für Details).

## Dark-/Hell-Modus-Umschalter (v1.22, 2026-08-15)

Manueller Umschalter im Info-Sheet ergänzt (überschreibt `prefers-color-scheme`), Standard-Pattern aus `PKA/BKM/PWA-Standards.md`. `theme-color`-Meta auf ein Tag konsolidiert, kein SW-Cache-Bump nötig (network-first HTML).

## Pitfalls

- **⚠️ Flask-Bindung war auf `0.0.0.0` statt `127.0.0.1` (gefunden + gefixt 2026-08-21):** `app.run(host=...)` lauschte auf allen Interfaces statt nur localhost – abweichend vom Muster aller anderen Flask-Apps hier (nginx proxied ohnehin nur gegen `127.0.0.1:5005`, s.o.). War nur durch UFW-Default-Deny auf Port 5005 abgesichert, kein explizites Nginx-only-Pattern. Fix: `host="127.0.0.1"`. Verifiziert: `ss -tlnp` zeigt jetzt `127.0.0.1:5005` statt `0.0.0.0:5005`, direkter externer Zugriff auf Port 5005 schlägt fehl, `/sentiment/` über nginx funktioniert weiterhin. **Läuft weiterhin über Flasks eingebauten Dev-Server** (nicht gunicorn wie die anderen Apps) – bewusst noch nicht migriert, da der eingebaute `apscheduler` (Scans alle 15 Min) bei mehreren gunicorn-Workern mehrfach parallel laufen würde (Duplikate/Mehrkosten) – bräuchte `-w 1` oder Scheduler-Auslagerung, siehe PKA-Todo.
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
- **`_tg_post()` splittet Nachrichten >4096 Zeichen automatisch** (siehe `PKA/BKM/Telegram-Integration.md`)
- **Scheduler-Jobs gestaffelt (2026-07-08):** Portfolio-Scan (`:12/:27/:42/:57`), EDGAR-Insider (`:00/:15/:30/:45`), 13D/13G (`:04/:19/:34/:49`) und Instant-Alerts (`:08/:23/:38/:53`) laufen bewusst NICHT mehr zur selben Minute – sonst konkurrieren sie um denselben globalen Finnhub-Throttle (`scanner._throttle()`, 55 Calls/Min). **Regel:** Neue 15-Min-Jobs, die Finnhub aufrufen, in dieses Raster einordnen (freie ~4-Min-Slots suchen), nicht auf `*/15` oder `0,15,30,45` zurückfallen.
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
- **`scan_enabled` hat Vorrang vor allem anderen (2026-08-06):** `_do_portfolio_scan()`/`_do_full_scan()` prüfen `scan_enabled` als allererstes – steht es auf `false` (Toggle „Scan pausieren" in den Einstellungen), passiert weder automatisch noch beim manuellen „Jetzt aktualisieren"-Button irgendetwas, ohne sichtbaren Fehler. Live gefunden: `scan_enabled` stand seit 2026-07-08 auf `false` (vermutlich vergessen), alle Portfolio-Kurse waren seitdem eingefroren (`current_price == buy_price`). Bei „Kurs aktualisiert sich nicht"-Meldungen zuerst `GET /api/config` prüfen, nicht nur den Scan-Code.
- **Datei-Owner nach `git pull` als root (behoben 2026-08-14):** Der Deploy-Befehl lief bisher ohne `chown` – ein `git pull` als `root` setzt den Owner aller dabei neu geschriebenen Tracked-Dateien auf `root:root`, der Service läuft aber als `webhook`. Live-Symptom: `POST /sentiment/api/portfolio` (Position hinzufügen) schlug mit `PermissionError: [Errno 13] Permission denied: 'portfolio.json'` fehl (`_update_portfolio()` → `path.touch()`), weil `portfolio.json` (gitignored, aber einmal manuell als root berührt) nicht mehr von `webhook` beschreibbar war – betraf zusätzlich mehrere Tracked-Dateien (`app.py`, `config.default.json`, `costs.py`, `forward_tracker.py`, `CLAUDE.md`). Fix: einmalig `chown -R webhook:webhook /opt/sentiment-scanner`, **Deploy-Befehl jetzt dauerhaft mit `chown -R webhook:webhook` nach jedem `git pull`** (siehe „Deployment" oben) – sonst reproduziert sich der Bug beim nächsten Pull.
- **Manueller Portfolio-Scan war zusätzlich an Handelszeit gebunden (behoben 2026-08-06):** `api_portfolio_scan_trigger()` rief `_do_portfolio_scan()` auf, das auch für den manuellen Trigger `_market_open()` (NYSE 9:30–16:00 America/New_York) prüfte – Klick auf „Jetzt aktualisieren" außerhalb der Handelszeit startete den Thread, der sofort ohne Aktion zurückkehrte. Fix: `_do_portfolio_scan(force: bool = False)`, der manuelle Trigger ruft mit `force=True` auf und umgeht damit nur den Marktzeit-Guard (nicht `scan_enabled`), der automatische Scheduler-Job bleibt unverändert marktzeit-gebunden.
- **Kurs-Update war an erfolgreichen Sentiment-Fetch gekoppelt (behoben 2026-08-06):** `_run_portfolio_scan_inner()` rief `_fetch_quote()` zwar für jeden Ticker auf, verwarf das Ergebnis aber per `continue`, sobald `_fetch_sentiment()` fehlschlug (live beobachtet bei mehreren Tickern durch Finnhub-503 auf `/company-news`) – der bereits erfolgreich abgerufene Kurs wurde dann nicht gespeichert. Fix: Kurs-Update-Block steht jetzt vor der sentiment-abhängigen Logik.
- **`price_stale`-Flag (2026-08-06):** Schlägt die Kursabfrage (`/quote`) in `_run_portfolio_scan_inner()`/`_update_portfolio_quotes()` fehl, bleibt `current_price` auf dem letzten bekannten Wert stehen, aber `entry["price_stale"] = True` wird gesetzt (bei Erfolg `False` + `price_updated_at`). Ohne diese Markierung war ein fehlgeschlagener Kurs-Fetch (API-Fehler) im Frontend nicht von einem tatsächlich unbewegten Kurs zu unterscheiden – Josef-Feedback. `pwa/index.html` zeigt bei `price_stale:true` ein ⚠-Badge mit Tooltip neben „Aktuell".
- **Retry-Pass für fehlgeschlagene Kurs-Fetches (2026-08-06):** Josef-Frage „können 503-Fehler mit einem zweiten Lauf finalisiert werden?" – Finnhub-Fehler sind meist transient. Beide Portfolio-Scan-Funktionen sammeln jetzt `price_stale`-Ticker während des Hauptlaufs und probieren sie direkt im Anschluss noch einmal (nur Kurs, nicht das komplette Sentiment). Logik in `_apply_price()`-Helper gebündelt (schreibt Kurs/P&L + setzt `price_stale`), von Hauptlauf UND Retry-Pass genutzt – kein duplizierter Code.

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

## Tages-Cap für Instant-Alerts + Einstellungen-UI + Header-Umbau (2026-07-09)

Josef-Feedback: (1) Header/Tab-Leiste war zu voll → „Einstellungen" aus der Tab-Leiste raus, als Zahnrad-Icon neben den Info-Button in den Header. (2) Zu viele Telegram-Signale von `check_instant_alerts()` (jedes starke Einzelsignal löste unabhängig und unbegrenzt aus) → Tages-Cap auf die stärksten 5. (3) Die Zahl im Alert („vermutlich der z-Score") war unklar → Erklärung ergänzt. (4) Alle `early_signals`-Werte waren nur direkt in der Server-`config.json` änderbar, nicht über die PWA.

- **Header:** `#tab-config` ist jetzt ein Icon-Button (Zahnrad-SVG) in `.hd-actions` neben `#btn-info`, nicht mehr in `.tabs`. Behält bewusst die ID `tab-config`, weil `switchTab()` per `document.getElementById('tab-' + name)` darauf zugreift – Verschieben ja, Umbenennen hätte einen JS-Fix gebraucht. Tab-Leiste hat jetzt nur noch 4 Einträge (Dashboard, Portfolio, Kosten, Früh).
- **Tages-Cap (`early_signals.max_instant_alerts_per_day`, Default 5):** `check_instant_alerts()` sortiert Kandidaten pro 15-Min-Lauf zuerst nach Rohwert (score) absteigend, dann vergleicht es pro Kandidat den heutigen Top-N-Bestand (`alerts` WHERE `kind='instant'` AND `alert_ts` >= Start des Handelstags America/New_York) gegen den neuen Score. Ist noch ein Slot frei ODER der Kandidat stärker als der bisher schwächste der heutigen Top-N → Alert wird ausgelöst. **Wichtig:** Bereits gesendete Telegram-Nachrichten werden nicht zurückgeholt – bei mehreren „stärker als der bisherige Schwächste"-Fällen an einem Tag kann die tatsächliche Nachrichtenzahl über 5 liegen, das Tageskontingent begrenzt die Zahl der *dauerhaft als Top-5-des-Tages geführten* Alerts, nicht hart die Anzahl der Telegram-Pushes. Gilt nur für `kind='instant'`, nicht für den täglichen Kombi-Alert aus `run_scoring()` (`kind='combo'`) – der ist durch die eigenen Schwellen (≥2 Signal-Typen, Mindest-Score) schon selten. **Live bestätigt (2026-07-09):** 5 unabhängige `volume_anomaly`-Signale (Microcaps, 60–380x Normalvolumen) feuerten im selben 15-Min-Lauf (21:23 UTC) + 1 weiterer Alert (ENR) bereits mittags → 6 Telegram-Nachrichten an einem Handelstag, 5 davon in einem Moment. Rohdaten unauffällig, kein Rechenfehler. Josef-Entscheidung nach Rückfrage: Verhalten bewusst so belassen (kein harter Cap, kein Cooldown pro Lauf).
- **Neue Spalte `alerts.kind`** (`'instant'` | `'combo'`) in `signals_db.py`, per `ALTER TABLE ... ADD COLUMN` in `init_db()` nachgezogen (bestehende `alerts.db` auf dem Server hatte die Spalte noch nicht – `CREATE TABLE IF NOT EXISTS` legt fehlende Spalten in einer schon existierenden Tabelle nicht nach). **Regel:** Schema-Änderungen an bereits produktiv befüllten Tabellen brauchen eine explizite Migration in `init_db()`, nicht nur eine geänderte `CREATE TABLE`-Anweisung.
- **Einstellungen-UI:** Neuer Abschnitt „Frühsignale" im Einstellungen-Tab mit allen `early_signals`-Feldern (inkl. `enabled`, `auto_watch`-Schieber, `max_instant_alerts_per_day`). `saveConfig()` baut den `early_signals`-Block jetzt explizit aus den Formularfeldern (analog zum bestehenden `filter`-Block) – überschreibt den beim `loadConfig()` gemerkten `_cfg`-Wert vollständig, alle 13 Keys sind im Formular abgedeckt.
- **Pitfall „Aktuell:"-Werte betrifft jetzt auch Frühsignale:** Die statischen Zahlen im Info-Sheet-Abschnitt „Frühsignale" (z.B. „Volumen-z-Score ≥ 6", „Kauf ≥ $100.000") sind jetzt über die neue Einstellungen-UI änderbar, aber der Info-Text bleibt hartcodiert – bei künftigen Wert-Änderungen über die PWA driftet die Erklärung wie beim bestehenden Filter-Pitfall (siehe oben) auseinander.
- **Zahl im Alert erklärt:** Info-Sheet + `title`-Tooltip auf `.es-alert-score` weisen jetzt explizit darauf hin, dass die angezeigte Zahl der unnormierte Rohwert des jeweiligen Signaltyps ist (nicht zwischen Typen vergleichbar) – bewusst keine Normierung eingeführt (hätte historische `total_score`-Werte inkonsistent zu neuen gemacht), nur die Erklärung ergänzt.

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
- **Pitfall yfinance:** `yf.download(tickers=[...], group_by="ticker")` liefert bei Listen- UND bei Einzel-String-Übergabe IMMER MultiIndex-Spalten. Bei Liste: `data[sym]["Volume"]` (auch bei 1 Ticker im Chunk). Bei Einzel-Ticker-String (kein `group_by`, wie im Forward-Tracker): `hist["Close"]` ist ein **DataFrame**, nicht Series → `hist["Close"][ticker]` nötig, sonst crasht `float(...)` für jeden Ticker (verifiziert 2026-07-06, Spec hatte hier einen Fehler). **Seit 2026-08-06 zentral in `yf_helper.fetch_closes()`** (ADR-010) – `forward_tracker.py` und `scan_tracker.py` nutzen beide diese eine Stelle, ein künftiger Fix muss nicht mehr an zwei Stellen gepflegt werden.
- `_day_counts` (wie `_news_texts`) nie persistieren – wird vor results.json/portfolio.json gestrippt

## Wöchentliche Performance-Analyse (2026-08-06)

Josef-Wunsch: wöchentlich sehen, was positiv vs. negativ performende Empfehlungen gemeinsam haben, getrennt für Sentiment-Scan und Frühsignale (unterschiedliche Feature-Sets/Zeithorizonte), um die Scoring-Logik ggf. neu bewerten zu können.

- **Datenmodell (`signals_db.py`):** Neue Tabellen `scan_snapshots` (Score-Komponenten je Top-N-Ticker pro Vollscan – existierte bisher nicht, `results.json` wird bei jedem Scan überschrieben), `scan_forward_returns` (Kursrendite 1/5/20 Handelstage später, analog `forward_returns`), `weekly_reports` (gespeicherte Analyseergebnisse inkl. optionalem KI-Text). Bewusst eigene Tabellen statt Erweiterung von `alerts`/`forward_returns` – Begründung in ADR-009. Alle drei bleiben von `cleanup_old_data()` unangetastet (Validierungshistorie, wie `alerts`/`forward_returns`).
- **`_write_scan_snapshot()`** in `scanner.py`, aufgerufen direkt nach `_write_results()` in `_run_scan_inner()` (nur im nicht-aborted-Zweig) – schreibt die Top-N-Ergebnisse zusätzlich in `scan_snapshots`.
- **Referenzkurs-Entscheidung:** `price_at_snapshot` kommt NICHT von einem extra Finnhub-Call, sondern wird von `scan_tracker.py` beim ersten Tracker-Lauf aus derselben yfinance-Tages-Close-Reihe entnommen wie der Forward-Preis (`closes.iloc[0]`) – 0 zusätzliche Kosten, siehe ADR-009.
- **`scan_tracker.py`** (neuer Scheduler-Job, Mo–Fr 18:00 America/New_York, 15 Min nach `es_tracker`): Pendant zu `forward_tracker.py`, füllt `scan_forward_returns`. Nutzt `yf_helper.fetch_closes()` (ADR-010).
- **`weekly_analysis.py`:** Regelbasierte Gruppenanalyse (0 Kosten) – fester Schwellenwert (`weekly_analysis.sentiment_pos/neg_threshold_pct` Default ±10, `early_signals_pos/neg_threshold_pct` Default ±15 in `config.json`) statt Quartile, da über Wochen stabiler interpretierbar; Fallback auf oberstes/unterstes Quartil bei Gruppengröße <5. Mindeststichprobe `MIN_SAMPLE=15` gereifte Datenpunkte (Horizont 20 Handelstage) je System, sonst `insufficient_data` + ETA-Schätzung aus der Reifegeschwindigkeit der letzten 4 Wochen. Neuer Scheduler-Job `weekly_analysis` (Mo 06:50 America/New_York) verschickt eine Telegram-Zusammenfassung beider Systeme.
- **Realistische Timeline:** Erste aussagekräftige Ergebnisse erst **~5–6 Wochen nach Deploy** (20 Handelstage Reifezeit + Mindeststichprobe) – vorher zeigt der Analyse-Tab/Telegram-Bericht „noch nicht genug Daten".
- **Optionaler KI-Button (`generate_ai_text()`):** Löst reale Claude-Haiku-Kosten pro Klick aus, nutzt bestehendes `costs.py`-Tages-Hard-Kill (`DAILY_HARD_KILL_USD`, gemeinsam mit dem bestehenden `ki_enabled`-Pfad des Hauptscans). Kein neuer Datendurchlauf – nur der bereits berechnete `weekly_reports.report_json` wird als Kontext an Claude übergeben. Fehlerpfade: kein `CLAUDE_API_KEY` oder `insufficient_data` → `ValueError` (400), Tageslimit erreicht → `RuntimeError` (429).
- **Endpoints:** `GET /api/analysis/latest?system=sentiment|early_signals`, `POST /api/analysis/run` (regelbasiert, sofort, kostenlos), `POST /api/analysis/run-ai` (Claude, kostenpflichtig, Frontend zeigt vorher einen `confirm()`-Kostenhinweis).
- **PWA:** 5. Tab „Analyse" (nach „Früh") mit System-Umschalter (Chip-Toggle, wiederverwendet `.es-filter-chip`), Kennzahlen-Vergleich Positiv-/Negativ-Gruppe, zwei Buttons.
- **Pitfall `get_latest_report()`:** Sortierung muss nach `id DESC`, nicht nach `report_ts DESC` – `report_ts` hat nur Sekundenpräzision, zwei Analysen derselben Sekunde (z.B. manueller Re-Run kurz nach dem automatischen Job) wären sonst mehrdeutig geordnet (live im Test gefunden und gefixt).
- **POST-Body-Konvention:** Wie bei allen bestehenden POST-Endpoints `request.get_json(force=True, silent=True)` verwenden – das Frontend setzt nirgends explizit `Content-Type: application/json`, `force=True` umgeht den sonst nötigen Mimetype-Check.
- **`_suggest_adjustments()` (2026-08-06, Josef-Wunsch nach dem VYNE-Fund):** Vergleicht Positiv-/Negativ-Gruppen-Mittelwerte je Metrik (≥20% relative Differenz) und schlägt bei bestätigter Richtung (`raise_threshold`) einen konkreten neuen Wert für den zugehörigen `config.json`-Schwellenwert vor (grobe Faustregel: Mittelwert von Positiv-/Negativ-Gruppe). **Wichtig:** Bei widersprüchlicher Richtung (`unexpected_inverse` – die Negativ-Gruppe hat den eigentlich „stärkeren" Wert) wird **kein** naiver „Schwelle anheben"-Vorschlag gemacht, da das in diese Richtung falsch wäre. Live-Befund direkt beim ersten echten Lauf: Frühsignal-Alerts mit extrem hohem Volumen-z-Score (Negativ-Gruppe Ø 82,8) performten in der ersten Stichprobe (n=6/6) schlechter als Alerts mit moderatem z-Score (Positiv-Gruppe Ø 14,7) – Auslöser war der VYNE-Alert vom 2026-07-10 (z-Score 80,56, 102× Normalvolumen, „Score 81" in den Logs war dieser rohe Signalwert, nicht der 0–100-Sentiment-Score – nicht verwechseln). Jeder Vorschlag trägt eine Konfidenz-Einordnung (`hoch`/`mittel`/`niedrig` nach Stichprobengröße, keine Signifikanztest) – bei kleinen Samples ist das ein erster Hinweis, keine Handlungsgarantie.

## Erweiterte Datensammlung für die Analyse (2026-08-06)

Josef-Frage: „Sammeln wir die richtigen Informationen? Nur der Score ist zu wenig für eine Muster-Analyse." Antwort: `scan_snapshots` speicherte bereits 7 Einzelkomponenten (nicht nur den Score), aber zwei echte Lücken gefunden und geschlossen:

- **`claude_confidence`:** Wird in `_claude_enrich_batch()` (Stufe 1b) bereits berechnet (`c["claude_confidence"] = ...`), war aber bisher nirgends persistiert – jetzt Teil von `scan_snapshots`.
- **`avg_volume_10d`, `avg_volume_3m`, `week52_high`, `week52_low`, `beta`:** Aus derselben `/stock/metric`-Antwort, die für jeden Top-N-Kandidaten ohnehin schon abgerufen wird (MarketCap+KGV) – 0 zusätzliche Finnhub-Calls, nur mehr aus dem bereits bezahlten Response extrahiert. **Feldnamen live verifiziert** (2026-08-06, temporärer Debug-Endpoint `/api/_debug_metric`, nur öffentliche Marktdaten zurückgegeben, sofort wieder entfernt, nie committed) statt geraten: `10DayAverageTradingVolume`, `3MonthAverageTradingVolume`, `52WeekHigh`, `52WeekLow`, `beta` – Werte in **Millionen Aktien** bei den Volumen-Feldern.
- **`sector` (`finnhubIndustry`), `float_shares` (`floatingShare`):** Neuer `/stock/profile2`-Call, **bewusst erst nach der Top-N-Selektion** (`SCAN_STATUS["phase"]="sektor"`, neue Phase, PWA zeigt dafür 99% statt irreführender 100%) statt in Stufe 2 – begrenzt die Zusatzlast auf ~`top_n_results` statt ~50–150 Kandidaten. `float_shares` ist explizit relevant für Penny-Stock-Volatilität (niedriger Streubesitz = leichter zu bewegen).
- **Migration:** `scan_snapshots` hatte beim ersten Rollout (selber Tag) noch keine dieser Spalten – `init_db()` migriert per `ALTER TABLE ADD COLUMN` analog dem bestehenden `alerts.kind`-Präzedenzfall.
- **`weekly_analysis.py`:** `_SENTIMENT_VALUE_COLS`/`_SENTIMENT_LEVERS` um die neuen Felder erweitert. `claude_confidence`/`avg_volume_10d` sind aktive Vorschlags-Hebel (kein config.json-Schwellenwert vorhanden, aber Kandidat dafür); `beta`/`avg_volume_3m`/`float_shares` sind vorerst nur deskriptiv (zu schwache Vorab-Hypothese für einen automatischen Vorschlag). Neue `sector_distribution_pct` pro Gruppe (z.B. „Positiv-Gruppe 60% Biotechnology").

## Cross-System-Analyse (2026-08-06)

Neue dritte Analyse-Dimension `cross_signal` (neben `sentiment`/`early_signals`): prüft, ob ein Ticker, der **innerhalb von ±7 Tagen sowohl im Sentiment-Scan als auch als Frühsignal-Alert** auftaucht, im Schnitt anders performt als einer ohne diese Überschneidung. Rein SQL (`julianday()`-Differenz zwischen `scan_snapshots.snapshot_ts` und `alerts.alert_ts`), 0 Kosten.

- **`_analyze_cross_signal()`** in `weekly_analysis.py`: eigene Mindestgröße für die Overlap-Gruppe (`_CROSS_MIN_OVERLAP=3`), zusätzlich zur normalen `MIN_SAMPLE=15`-Schwelle – Überschneidungen sind naturgemäß seltener als die Gesamtmenge.
- **Report-Form unterscheidet sich bewusst** von `sentiment`/`early_signals`: `overlap_group`/`no_overlap_group` statt `pos_group`/`neg_group`, kein `thresholds`/`grouping_method`/`suggestions`-Feld (macht in diesem Kontext keinen Sinn – hier wird nicht nach Rendite gruppiert, sondern nach Systemüberschneidung). `analyze_and_store()` ruft `_suggest_adjustments()` deshalb nur für `sentiment`/`early_signals` auf, nicht für `cross_signal`.
- **PWA:** dritter System-Toggle „Cross-Signal" im Analyse-Tab, eigene `anCrossCard()`-Rendering-Funktion (einfacher als `anGroupCard()`, da keine Score-Komponenten-Vergleiche nötig).
- **`app.py`-Endpoints:** alle drei (`/latest`, `/run`, `/run-ai`) akzeptieren jetzt `system=cross_signal` zusätzlich zu `sentiment`/`early_signals`.

## Frühsignal-Nachrichtenflut behoben (2026-08-09)

Josef-Feedback: am 07.08. kurz vor Mitternacht über 100 Frühsignal-Telegram-Nachrichten erhalten – „das ist mir zu viel, kann ich nicht auswerten und mach damit eher gar nichts".

- **Root Cause (aus `scan.log` rekonstruiert):** `run_scoring()` (täglicher Kombi-Lauf, 17:35 ET) erzeugte an diesem Abend **185 Alerts in einem Durchlauf** (+5 Instant-Alerts = 190). Ursache: `layer2_volume.py` berechnete den Volumen-z-Score als `(today_vol - mean_v) / sd_v` ohne Plausibilitätsprüfung der Standardabweichung – bei einem Ticker mit nahezu konstantem Tagesvolumen wird `sd_v` numerisch winzig, wodurch `z` bei jeder kleinsten Abweichung explodiert (live beobachtet: `z=11598` für „MB", `z=1719` für „WFF" – statistisch nicht plausibel, kein echtes Marktsignal, klassischer Fall einer instabilen Division durch eine Fast-Null-Standardabweichung).
- **Fix 1 (Signal-Qualität):** Mindest-Variationskoeffizient `sd_v >= mean_v * 0.05` als statistische Untergrenze in `layer2_volume.py`, bevor der z-Score als Anomalie gilt.
- **Fix 2 (Notification-Architektur, unabhängig von Fix 1):** `_create_alert()` verschickt selbst kein Telegram mehr, gibt stattdessen `{"ticker", "total_score"}` zurück (oder `None` bei aktivem Cooldown). `run_scoring()`/`check_instant_alerts()` sammeln alle in einem Lauf erstellten Alerts und verschicken über den neuen `_send_digest()`-Helper **genau eine** Sammel-Nachricht pro Lauf (Ticker-Liste, bei >8 Tickern „+N weitere", Verweis auf den Früh-Tab für Details) – das begrenzt die Nachrichtenzahl pro Lauf hart auf 1, unabhängig davon ob Fix 1 jede denkbare künftige Ursache für einen Signal-Burst abdeckt (Verteidigung in der Tiefe).
- **`_detail_line()`** (nur für die alte ausführliche Einzelnachricht gebraucht) entfernt – Signal-Details stehen weiterhin vollständig im Signal-Feed der PWA (`/api/early-signals`), nur nicht mehr in der Telegram-Nachricht.
- Lokal funktional getestet (simulierter 8er- und 185er-Burst, jeweils genau 1 Telegram-Nachricht, korrekte „+N weitere"-Kürzung), deployed.

## Backfill des letzten historischen Scans (einmalig, 2026-08-06)

Josef-Wunsch: Datensammlung ein paar Wochen rückwirkend nachholen, um nicht 5-6 Wochen auf erste Ergebnisse warten zu müssen. **Echtes Backfill für den Sentiment-Scan ist NICHT sauber möglich** und wurde deshalb bewusst nicht gebaut:

- `/stock/metric` (MarketCap, KGV, Volumen, Beta) liefert bei Finnhub Free-Tier nur den **aktuellen** Wert, keine Zeitreihe – ein nachträglicher Abruf für einen 3 Wochen alten Snapshot würde heutige Werte in einen historischen Kontext mischen (Look-Ahead-Bias), was die Analyse verfälschen statt verbessern würde.
- `results.json` wird bei jedem Vollscan überschrieben – es existiert kein Archiv der Top-N-Listen früherer Tage. Log-Historie (`scan.log`) zeigt 21 vollständige Scans zwischen 2026-06-22 und 2026-07-06 (danach Lücke bis 2026-08-06 durch den `scan_enabled`-Bug), aber nur die Zusammenfassung („N Ergebnisse"), keine Ticker-Details – nicht rekonstruierbar.
- **Einzige saubere Ausnahme:** Der allerletzte vollständige Scan (2026-07-06, 16:33 UTC, 10 Ticker) lag noch vollständig und unverzerrt in `results.json` vor (alle Felder wurden damals erfasst, keine nachträgliche Aktualisierung nötig). Einmalig per Skript in `scan_snapshots` nachgetragen (`signals_db.insert_scan_snapshots()` direkt aufgerufen), danach `scan_tracker.run_scan_tracker()` einmalig manuell ausgeführt – das 20-Handelstage-Fenster war bereits verstrichen, alle Renditen sofort verfügbar (u.a. JLHL +68,3%, war zwischenzeitlich bei +124,7% nach 5 Tagen).
- **Ergebnis:** `sample_size` für `system=sentiment` sprang von 0 auf 10 (weiterhin `insufficient_data=true`, `MIN_SAMPLE=15`), `eta_weeks` zeigt jetzt eine reale Schätzung (~2 Wochen) statt `null`. Kein fingierter Fortschritt – reale, historisch korrekte Daten.
- **Josef hat explizit abgelehnt**, `MIN_SAMPLE` testweise zu senken oder Ergebnisse trotz zu kleiner Stichprobe anzuzeigen ("lieber ehrlich warten") – dabei bleibt es, keine Preview-Modus-Implementierung.
- **Telegram-Historie als mögliche weitere Quelle besprochen, aber nicht umgesetzt:** Bot-API kann keine gesendete Nachrichten-Historie auslesen (Plattform-Limitierung, keine Berechtigungsfrage) – Josef müsste ggf. über Telegram Desktops „Chat exportieren"-Funktion eine Datei bereitstellen, aus der sich weitere historische Top-5-Listen (22.06.–06.07., ca. 15 Scan-Läufe) extrahieren ließen. Offen, kein Termin vereinbart.

## Telegram-Chatverlauf-Backfill (2026-08-09)

Nach dem 06.07.-Einzel-Backfill (siehe oben) fragte Josef, ob wir näher an die Mindeststichprobe kommen. Bot-API kann keine gesendete Nachrichten-Historie auslesen (Plattform-Limitierung) – Josef hat stattdessen den Chatverlauf über Telegram Desktop exportiert (Menü „Chatverlauf exportieren" → HTML, `~/Downloads/Telegram Desktop/ChatExport_.../messages*.html`) und die Datei bereitgestellt.

- **Extraktion:** Regex-Parser über die exportierten HTML-Dateien (`<div class="message" id="messageN">`-Blöcke), gefiltert auf Nachrichten mit „Stock Sentiment Scan". Die exakte UTC-Scanzeit steht direkt im Nachrichtentext (`Stock Sentiment Scan — HH:MM UTC`) – keine Zeitzonen-Rateraten nötig trotz Telegrams Anzeige in einem festen `UTC+01:00`-Offset (der nicht DST-adjustiert war und deshalb ~1h von der echten Serverzeit abwich).
- **Gefunden:** 16 Sentiment-Scan-Nachrichten (22.06.–07.08.), davon 3 mit 0 Ergebnissen, 1 Duplikat des bereits per `results.json` nachgetragenen 06.07.-Scans (16:33 UTC), 1 bereits vom laufenden Betrieb erfasster Scan (07.08., nach dem `scan_enabled`-Fix), **10 echte neue Scans** (24.06.–06.07., je Top 5 = 50 Ticker-Zeilen).
- **Wichtige Einschränkung:** Telegram-Nachrichten enthalten nur `ticker, name, score, bullish_pct, buzz, market_cap, articles_week` – NICHT `bearish_pct, sentiment_norm, pe, claude_confidence` (die stehen nur in der vollständigen `results.json`, nicht im Kurzbericht). Diese Felder bleiben für die Telegram-Backfill-Zeilen `NULL`, `_group_stats()`/`_mean()` filtern `None`-Werte bereits automatisch heraus – kein Sonderfall nötig.
- **Live gefundener Produktionsbug beim Nachtrag:** `yf_helper.fetch_closes()` gab bei einem yfinance-Ergebnis ohne Kursdaten (z.B. delisteter Ticker) eine **leere** Series statt `None` zurück – `scan_tracker.py` stürzte dann mit `IndexError` auf `closes.iloc[0]` ab und brach den kompletten Tracker-Lauf für ALLE offenen Snapshots ab (nicht nur den betroffenen Ticker). Betraf hier `VYNE`/`OLPX`/`SEM`/`ESPR`/`STEL` aus dem 08.07.-Scan. Gefixt: `fetch_closes()` prüft jetzt explizit auf eine leere Series und gibt `None` zurück – der bestehende `if closes is None: continue`-Guard in beiden Trackern greift dann korrekt. Dieser Bug hätte auch reguläre Scheduler-Läufe des `scan_tracker`-Jobs unbemerkt lahmgelegt, sobald ein delisteter Ticker im Backlog war.
- **Ergebnis:** `scan_snapshots` 194→244 Zeilen, `horizon=20`-gereifte Datenpunkte 10→60 – **über der Mindeststichprobe von 15**. Erste echte Analyse lieferte einen Befund (Konfidenz „mittel"): Negativ-Gruppe hatte höheren Buzz (Ø 3,95) als Positiv-Gruppe (Ø 1,93) – widerspricht „mehr Buzz = stärkeres Signal", ähnliches Muster wie der VYNE-Volumen-z-Score-Befund bei den Frühsignalen (bereits hoch gehypte Ticker tendenziell schwächer).
- **Temporäre Backfill-Datei** (`/tmp/telegram_backfill.json`, lokal und auf dem Server) nach Verwendung gelöscht – kein dauerhaftes Skript, war ein einmaliger manueller Vorgang.

## Layer 6: Tages-Konsolidierung + Verkaufssignal + atomare Portfolio-Writes (2026-08-09)

Fable-Review zur Frühsignal-Logik (auf Josefs Wunsch, nach der Nachrichtenflut vom selben Tag): Josef will keine einzelnen Alerts mehr sichten, sondern höchstens **einen** Kauf-Pick pro Handelstag über beide Systeme (Frühsignale + Sentiment-Scan) hinweg, mit nachvollziehbarer Begründung, plus ein symmetrisches Verkaufssignal für echte Positionen. Der Plan wurde vor Umsetzung von einem unabhängigen Fable-Review gegengeprüft – 6 reale Bugs gefunden, alle eingearbeitet (siehe ADR-011/012/013).

- **`layer6_daily_pick.py`** (neu, Scheduler-Job `daily_pick`, Mo–Fr 18:05 America/New_York, nach `scan_tracker`): Kandidaten = alle `alerts` + `scan_snapshots` des laufenden Handelstags. **Bestätigung** (mind. eine): C1 `kind='combo'` ODER ≥2 unterschiedliche `signal_type` in 7 Tagen (`source='early_signals'`), C2 Ticker in Alert UND Snapshot innerhalb `_DAILY_PICK_CROSS_DAYS=2` Tagen (`source='cross_signal'`, eigene Konstante – NICHT `weekly_analysis._CROSS_OVERLAP_DAYS`, der ist für retrospektive Monats-Statistik kalibriert), C3 Snapshot-`rank<=5` UND `bullish_pct>=70` (`source='sentiment_scan'`). **Veto:** `volume_anomaly`-z-Score > `daily_pick.max_volume_z` (Default 30, Plausibilitätsgrenze nach dem VYNE-/11598-Vorfall), frisches `insider_sell`-Signal (7 Tage, gleicher Mindestbetrag wie Käufe), Snapshot `bearish_pct > bullish_pct`, Liquidität unter `min_avg_volume_10d`/`min_float_shares` (aus Snapshot, sonst 1 zusätzlicher `/stock/metric`+`/stock/profile2`-Call für den Finalisten), Repick innerhalb `repick_cooldown_days` (Default 5). **Ranking:** feste Quell-Priorität `cross_signal > early_signals > sentiment_scan` statt direktem Score-Vergleich (additive Frühsignal-Scores ~2-10 und Sentiment-Scan-Score 0-100 sind nicht vergleichbar – ADR-013), zusätzlich Mindest-Score je Quelle (`min_score_early_signals`/`min_score_sentiment_scan`). `reasoning_json` enthält die konkreten Rohwerte je Signal (nicht nur Kriterien-Namen). **Idempotent** über `UNIQUE(daily_picks.pick_date)`; "kein Pick" ist ein valides, persistiertes Ergebnis mit eigener kurzer Telegram-Nachricht (nie Schweigen).
- **`layer6_sell_signal.py`** (neu, Scheduler-Job `sell_signal_check`, Minuten-Raster `1,6,11,16,21,26,31,36,41,46,51,56`, 9-16 Uhr ET Mo-Fr – NICHT `*/5`, das kollidiert garantiert mit `edgar_scan` auf 0/15/30/45): prüft NUR echte, bestätigte Positionen (`watch=false`), nicht die automatisch angelegten 👁-Beobachtungen (Josef-Klarstellung: "Verkaufssignale checken nur für bestätigte Käufe, nicht für alle Empfehlungen"). Neue `signals`-Einträge (letzte 10 Min) der Portfolio-Ticker: `insider_sell` löst immer aus, `volume_anomaly` nur zusätzlich mit frischem Kursrückgang (1 `_fetch_quote()`-Call pro Kandidat). Setzt dieselben `sell_signal`/`sell_reason`-Felder wie die bestehende Sentiment-Logik weiter – **kein neuer PWA-Code nötig** (Banner/`resetSignal()` funktionieren automatisch mit).
- **`insider_sell`-Signaltyp** (neu, `layer1_edgar.py`): `_parse_form4()` aggregiert Käufe (Code P/A) und Verkäufe (Code S/D) jetzt GETRENNT im selben XML-Durchlauf, gibt nur noch `None` zurück wenn beide leer sind – vorher brach die Funktion bei `total_usd<=0` sofort ab und hätte reine Verkaufs-Filings (der häufigste Fall) verworfen, bevor `insider_sell` als Gegensignal existierte. Gleicher Mindestbetrag (`insider_min_usd`) wie bei Käufen, sonst würden Routine-Verkäufe aus 10b5-1-Plänen praktisch täglich das Veto auslösen.
- **`sell_signal_source`-Feld** (neu, `entry["sell_signal_source"]`, `"sentiment"`|`"fruehsignal"`, fehlt=Alt-Verhalten=`"sentiment"`): Cross-Contamination-Fix. Vorher setzte `_run_portfolio_scan_inner()` `sell_signal=False` automatisch zurück sobald die Stimmung wieder gut aussah – UNABHÄNGIG von der Ursache. Ein Frühsignal-Sell-Flag (z.B. Insider-Verkauf) wäre dadurch stillschweigend gelöscht worden, obwohl der eigentliche Auslöser nie aufgelöst wurde. Der Auto-Reset in `scanner.py` löscht `sell_signal` jetzt nur noch, wenn die Quelle fehlt oder `"sentiment"` ist.
- **`scanner._update_portfolio(mutator_fn)`/`_merge_portfolio_updates()`** (BKM `Atomic-Write-Pattern.md`, Pflicht-Vorarbeit): `portfolio.json` hat mehrere nebenläufige Schreibquellen (Web-Endpoints, `run_portfolio_scan()` alle 15 Min, jetzt zusätzlich `layer6_sell_signal.py` alle 5 Min) – ohne Lock verliert der zuletzt schreibende Job stillschweigend die Änderungen des anderen. Lock (`threading.Lock` + `fcntl.flock`) + Tempfile + atomares Rename, Mutation läuft immer auf dem AKTUELLEN Dateiinhalt (nicht einer am Lauf-Anfang geladenen, ggf. veralteten Kopie). `api_portfolio_add/delete/update`, `_auto_watch()`, `_run_portfolio_scan_inner()`, `_update_portfolio_quotes()` nutzen jetzt ausschließlich diesen Helper.
- **`PATCH /api/portfolio/<ticker>/convert`** ("Zu echter Position machen", Josef-Wunsch): wandelt eine 👁-Beobachtung (`watch:true`, 1 Test-Aktie) in eine echte Position um – setzt `watch=false` + die tatsächlichen `shares`/`buy_price`/optional `buy_date`. 404 wenn Ticker nicht gefunden, 400 wenn bereits `watch:false`. PWA: Button auf jeder 👁-Karte, ~~`prompt()`-Dialoge für Anzahl/Kurs/Datum (kein neues Formular)~~ **seit 2026-08-21 stattdessen über das "Aktie hinzufügen"-Formular, siehe unten.**
- **`daily_picks`-Tabelle** (`signals_db.py`): referenziert `alerts`/`scan_snapshots` statt eigene Forward-Returns zu duplizieren (ADR-011), bleibt wie diese von `cleanup_old_data()` unangetastet (Validierungshistorie).
- **PWA:** neue "Heutiger Tages-Pick"-Karte oben im Dashboard (vor der Alert-Box), `loadDailyPick()`, zeigt volle Begründung inkl. Rohwerten oder "Kein Tages-Pick"-Hinweis.
- **Endpoints:** `GET /api/daily-pick/latest`, `POST /api/daily-pick/run` (`force=true` löscht+erneuert die heutige Zeile), `POST /api/sell-signal-check/run`.
- **Survivorship-Bias bewusst unadressiert:** Layer 6 konsumiert nur, was `tickers.csv`/Layer 1-5 bereits liefern – delistete Ticker fallen aus der Analyse, das strukturelle Problem wird hier nicht gelöst, nur dokumentiert.

## Kosten-Reiter → Header-Icon (2026-08-09)

Josef-Wunsch: Kosten aus der Tab-Leiste raus, entweder in den Info-Bereich oder als eigener Button daneben. Entscheidung nach Rückfrage: eigener Icon-Button im Header (nicht Info-Bereich) – Kosten zeigt Live-Daten (Fortschrittsbalken, KPIs, Scan-Historie), keine statische Erklärung wie der Info-Bereich, ein weiterer Header-Button war daher konsistenter.

- `#tab-costs` (Dollar-Icon, bisher in `.tabs`) wurde 1:1 nach `.hd-actions` verschoben, exakt nach dem `#tab-config`-Präzedenzfall (Zahnrad, seit 2026-07-09): gleiche ID beibehalten (`switchTab()` greift per `document.getElementById('tab-' + name)` darauf zu), `screen-costs`/`loadCosts()` unverändert. Kein JS-Change nötig, nur HTML-Position + CSS-Klassen (`#tab-config,#tab-costs{...}`-Selektoren geteilt).
- Tab-Leiste hat jetzt nur noch 4 Einträge (Dashboard, Portfolio, Früh, Analyse), Header 3 Icons (Einstellungen, Kosten, Info). Version 1.14 → 1.15.

## Firmeninfo-Button (2026-08-09)

Altes Todo (26.06.): Firmendetails/Bewertungsinfos beim Klick auf einen Ticker im Dashboard. Finnhub Free-Tier liefert keine Freitext-Unternehmensbeschreibung – Josef hat sich bewusst gegen einen zusätzlichen kostenpflichtigen Claude-Call entschieden (Option "nur strukturierte Fakten").

- **scanner.py:** Die bestehende Sektor-Anreicherungsschleife (`/stock/profile2`, läuft seit 2026-08-06 pro Top-N-Ticker) extrahiert jetzt zusätzlich `weburl`/`ipo`/`exchange`/`shareOutstanding`/`country` aus derselben, bereits bezahlten Response – **0 zusätzliche API-Calls**, exakt dasselbe Muster wie die Datensammlungs-Erweiterung vom 6.8.
- **PWA:** ℹ️-Button neben dem Ticker (Karte + Tabelle) öffnet ein Bottom-Sheet (`#ci-overlay`) mit allen bereits im Client geladenen `_results`-Feldern (Branche, MarketCap, KGV, 52W-Range, Beta, Ø-Volumen, Streubesitz, Aktien gesamt, Börsengang, Börsenplatz, Land, Website) – **kein neuer Fetch**, reines Rendering von schon vorhandenen Daten.
- **Pitfall Website-Link:** `r.weburl` stammt von Finnhub (extern) – vor dem Rendern als `<a href>` wird das Schema geprüft (`/^https?:\/\//i`), sonst wäre ein `javascript:`-URI theoretisch möglich, falls Finnhub (oder ein MITM) je einen manipulierten Wert zurückgäbe. **Regel:** Externe URL-Strings nie ungeprüft in `href` einsetzen, auch wenn `esc()` bereits Quotes/Tags escaped – das schützt nicht vor dem URI-Schema selbst.
- **Rollout-Verzögerung:** Die vier neuen Felder erscheinen erst ab dem NÄCHSTEN Vollscan nach dem Deploy in `results.json` – bestehende Einträge zeigen bis dahin nur die schon vorhandenen Felder (Branche/MarketCap/KGV), kein Fehler, nur `null` für die neuen Felder.

## Tägliche Performance-Analyse mit automatischer Basis-Schwellen-Übernahme (2026-08-09)

Josef-Wunsch: Die Analyse (bisher nur Montag früh) soll regelmäßig – mit einstellbarem Intervall – laufen, bei fundamentalen Erkenntnissen sofort per Telegram melden, und die daraus abgeleiteten Einstellungen automatisch übernehmen ("vollautomatisch", bewusst gewählt trotz Hinweis auf das Risiko bei aktuell noch kleiner Stichprobe).

- **Scheduler:** Job `weekly_analysis` (ID/Config-Block-Name aus Kompatibilitätsgründen unverändert, obwohl er jetzt nicht mehr wöchentlich läuft) läuft jetzt **täglich Mo-Fr 18:10 America/New_York** – direkt NACH `scan_tracker`(18:00)/`es_tracker`(17:45), die die frischen Kursrenditen des Tages nachtragen. Vorher (Montag 06:50 ET) hätte ein "nach dem Scan"-Trigger (Josefs ursprüngliche Formulierung) mit veralteten Renditedaten gerechnet, da der Vollscan morgens (12:20 UTC) läuft, die Tracker aber erst abends.
- **Selbst-Throttle statt dynamischem Rescheduling:** `weekly_analysis._should_run_today()` prüft bei jedem täglichen Lauf, ob seit dem letzten gespeicherten Report (Referenzsystem `sentiment`) `weekly_analysis.interval_days` (Default 1) Tage vergangen sind – sonst wird der Lauf übersprungen. Ändert Josef das Intervall in den Einstellungen, wirkt das sofort beim nächsten täglichen Job-Tick, ohne dass `_reschedule()`/der Scheduler-Job selbst angefasst werden muss.
- **Automatische Übernahme nur der Basis-Schwelle (ADR-014):** `weekly_analysis._apply_suggestions()` übernimmt bei einem `raise_threshold`-Vorschlag automatisch NUR den ersten `config_keys`-Eintrag via neuem `scanner._update_config()` (BKM Atomic-Write-Pattern, da `config.json` damit einen zweiten automatischen Schreiber neben `POST /api/config` bekommt). Weitere Keys desselben Vorschlags (z. B. bei den Frühsignal-Hebeln die höhere Einzelsignal-Schwelle `single_volume_z_min` neben der Basis-Schwelle `volume_z_min`) werden bewusst NICHT automatisch geändert – landen als `skipped_keys` auf dem Vorschlag, sichtbar in Telegram/Analyse-Tab. `unexpected_inverse`-Vorschläge werden nie automatisch übernommen (kein `suggested_value` vorhanden).
- **Diff-Erkennung für "fundamentale Erkenntnisse":** `weekly_analysis._new_findings()` vergleicht die Vorschläge (Schlüssel: `metric`+`kind`) gegen den jeweils letzten Report desselben Systems. Nur GENUINE neue Vorschläge lösen eine sofortige, eigene Telegram-Nachricht aus (`_send_new_findings_alert()`) – verhindert, dass dieselbe unveränderte Erkenntnis jeden Werktag erneut gemeldet wird.
- **Ausführliche Wochenzusammenfassung bleibt 1x/Woche:** `_send_weekly_summary()` (die bisherige, unveränderte Positiv-/Negativ-Gruppen-Übersicht) wird weiterhin nur montags verschickt (`datetime.now(_ET).weekday() == 0`), unabhängig vom Analyse-Intervall – sonst wäre das nach dem Nachrichtenflut-Fix vom selben Tag wieder eine tägliche Telegram-Dauerbeschallung gewesen.
- **Kein Auto-Scan-Trigger:** Bewusste Josef-Entscheidung (Kosten-Rückfrage, da ein automatisch ausgelöster Scan bei künftig aktivem `ki_enabled` reale Zusatzkosten verursachen würde) – stattdessen neuer manueller **"🔄 Jetzt neu scannen"**-Button im Analyse-Tab (nutzt den bestehenden `/api/scan`-Endpoint, keine neue Logik).
- **`config.default.json`:** neuer Key `weekly_analysis.interval_days` (Default 1). **Achtung:** `config.json` ist gitignored – bestehende Installationen (auch die Produktivinstanz) haben den `weekly_analysis`-Block ggf. noch gar nicht (vor 2026-08-06 angelegt); Code fällt in dem Fall überall auf `.get("weekly_analysis", {}).get(key, default)` zurück, funktioniert also auch ohne den Block, zeigt ihn aber erst in `GET /api/config`/der PWA, sobald einmal gespeichert wurde.
- **KI-Analyse-Button bleibt komplett unabhängig:** `generate_ai_text()` liefert nur interpretierenden Fließtext zum bereits berechneten Report, keine eigenen Zahlen/Vorschläge – fließt nicht in `_apply_suggestions()` ein und wird durch diesen Automatismus weder ausgelöst noch verändert.
- **PWA:** neues Einstellungen-Feld "Intervall (Tage)" (`weekly_analysis` als expliziter Block in `saveConfig()`, Spread von `_cfg.weekly_analysis` – Pitfall aus 2026-07-07 beachtet, jedes Top-Level-Feld muss beim Payload-Bauen erhalten bleiben), Analyse-Tab-Vorschlagskarten zeigen `✅ Automatisch übernommen: <key> = <wert>` bzw. `skipped_keys`, neue Info-Sheet-Sektion "Performance-Analyse", Version 1.15 → 1.16.

## Login-System: Flask-Session statt nginx Basic-Auth (2026-08-09, ADR-015)

Josef wollte das seit 2026-07-07 offene Todo (K4 aus dem Fable-Review: `GET /api/portfolio` zeigt Kaufpreise/P&L öffentlich, Schreib-Endpoints ungeschützt) endlich schließen. **nginx Basic-Auth wurde zuerst umgesetzt und LIVE wieder verworfen:**

- `auth_basic`/`auth_basic_user_file` in der `location /sentiment/` funktionierte über `curl` und im normalen Browser-Tab einwandfrei (401 ohne Credentials, `WWW-Authenticate`-Header korrekt), aber **die installierte Home-Bildschirm-PWA zeigte gar keinen Login-Dialog** – sofort ein nackter 401-Body. Root Cause: `pwa/sw.js` fängt `document`-Navigationen ab und macht selbst `fetch(e.request)` (network-first). `fetch()` folgt zwar Redirects automatisch, wirft aber bei einer 401-Antwort **keine Exception** – der Service Worker bekommt die 401-Response ganz normal zurück und rendert sie als Seiteninhalt, statt dass der Browser (wie bei einer direkten Top-Level-Navigation) den nativen Basic-Auth-Dialog öffnet. Ein bekanntes Zusammenspiel-Problem zwischen Service-Worker-Fetch-Interception und HTTP Basic-Auth, live verifiziert (nicht nur Theorie).
- Session-Cookies + normale HTTP-Redirects funktionieren dagegen zuverlässig innerhalb des SW-Fetch-Handlers (ein 302 auf `/login` wird transparent gefolgt, die Login-Seite wird ganz normal als Dokument gerendert) – deshalb jetzt **Flask-Session-Login nach dem bereits bewährten Claude-Remote-Muster** (`Claude-Remote/app.py`): `passlib.apache.HtpasswdFile` gegen dieselbe `/etc/nginx/sentiment.htpasswd` (Josef legt das Passwort selbst per `htpasswd -c` an, landet nie im Chat/Repo), `app.secret_key` aus `.session_key` (autogeneriert, `chmod 600`, gitignored), `@login_required`-Decorator auf **23 Routen** (Index `/sentiment/` + alle `/api/*`) – `manifest.json`/`sw.js`/Icons/`login`/`logout` bewusst **öffentlich** gelassen (keine sensiblen Daten, verhindert dass der SW beim allerersten Cache-Install eine Login-Weiterleitung statt der echten PWA-Shell cached).
- **Session-Dauer bewusst abweichend von Claude Remote:** 30 Tage Sliding-Window (`app.permanent_session_lifetime`, verlängert sich bei jeder Anfrage über `@app.before_request`) statt Claude Remotes 15 Minuten – dort geht es um SSH-/Server-Eingriffe (hohes Risiko bei gekapertem Cookie), hier nur um Portfolio-Einsicht.
- `requirements.txt`: `passlib>=1.7` ergänzt, auf dem Server nachinstalliert (`pip install -r requirements.txt`).
- Neue `pwa/login.html` (App-Farbschema, kein Claude-Remote-Lila), „Abmelden"-Button in den Einstellungen.
- **Pitfall für künftige Endpoints:** Jeder neue `@app.route()` MUSS manuell `@login_required` bekommen (kein automatischer Schutz wie bei nginx-Location-weitem Basic-Auth) – bei PR-Reviews/neuen Features aktiv gegenchecken.

## Convert-P&L-Bug (2026-08-09)

Screenshot-Fund: `api_portfolio_convert()` ("Zu echter Position machen") setzte `shares`/`buy_price` neu, ließ `current_value`/`pnl`/`pnl_pct` aber unangetastet – bis zum nächsten 15-Min-Portfolio-Scan zeigte die Karte noch die Zahlen aus dem alten 1-Aktie-Beobachtungszustand (Beispiel PRQR: 75 neue Aktien, aber Positionswert/P&L noch von der einen alten Test-Aktie). Fix: `_conv_mutator()` ruft direkt `scanner._apply_price(p, p.get("current_price"))` auf (sofortige Neuberechnung mit dem letzten bekannten Kurs, 0 API-Calls) und stößt zusätzlich einen Hintergrund-`run_portfolio_scan()` an (analog `api_portfolio_add()`) für einen wirklich frischen Kurs. **Bereits vor dem Fix umgewandelte Positionen bleiben falsch, bis entweder der nächste reguläre Scan läuft oder man sie manuell nachzieht** (einmalig für PRQR live per `_apply_price()`-Aufruf über `_update_portfolio()` korrigiert).

## EUR-Kaufpreis-Umrechnung (2026-08-09, ADR-016)

Kaufpreis kann beim manuellen Hinzufügen und bei „Zu echter Position machen" wahlweise in Euro angegeben werden – automatische Umrechnung zum **historischen Kurs des Kaufdatums** (nicht des Eingabedatums) in USD.

- `scanner._eur_to_usd_rate(date_str=None)`: Frankfurter.app (EZB-Referenzkurse, kostenlos, kein API-Key) – bewusst NICHT Finnhub (kein verlässlicher Free-Tier-Forex-Endpoint bekannt). Nicht-Handelstage (Wochenende) liefern automatisch den letzten verfügbaren Vortageskurs (kein Fehler, live verifiziert). Ungültiges/zu altes Datum → `{"message":"not found"}`, fängt ab und fällt auf `/latest` zurück. `None` nur wenn auch das fehlschlägt.
- `POST /api/portfolio` und `PATCH /api/portfolio/<ticker>/convert` akzeptieren optionales `currency` (Default `"USD"`). Bei `"EUR"`: `buy_price` wird als EUR-Betrag interpretiert, umgerechnet, der intern gespeicherte `buy_price` bleibt immer USD (keine Änderung an der P&L-Logik nötig). Zusätzlich `buy_price_eur`/`fx_rate_used` gespeichert (Transparenz), auf der Portfolio-Karte als Zusatzzeile angezeigt.
- **Live-Pitfall (eToro-Fund, PRQR-Beispiel):** Broker-Apps wie eToro zeigen den **Stückpreis oft ohne Währungssymbol bereits in der Handelswährung** (USD bei US-Aktien wie PRQR) – nur Summenfelder („Nettowert", „Gewinn") sind in die Kontowährung (€) umgerechnet. Josef hatte „75 Einheiten @ 1.75" als €1,75 gelesen, tatsächlich war es bereits $1,75 – die EUR-Umrechnung hätte daraus fälschlich einen Verlust statt des echten Gewinns gemacht. Live anhand des eToro-Screenshots aufgeklärt (Vorzeichen/Größenordnung von P&L stimmte mit der reinen-USD-Interpretation überein, nicht mit der EUR-Umrechnung). **Hinweistext bei der Währungsauswahl ergänzt** (Hinzufügen-Formular + Convert-Prompt): explizit vor dieser Broker-UI-Falle warnen. Bei künftigen manuellen Korrekturen: im Zweifel nachfragen statt aus einer Zahl ohne Symbol eine Währung zu raten.

## Verkaufsempfehlungen nur für echte Positionen (2026-08-09)

Live-Fund direkt nach dem Login-Rollout: Der **Sentiment-basierte** Verkaufscheck in `_run_portfolio_scan_inner()` lief bisher für ALLE Portfolio-Einträge, inklusive der vielen Auto-Watch-Beobachtungen (`watch:true`). Drehte sich die Stimmung bei mehreren Beobachtungen im selben Scan gleichzeitig, sendete `_send_telegram_sell()` pro Ticker **einzeln** (kein Digest-Muster wie bei `layer4_scoring._send_digest()`) – reale Nachrichtenflut (12 gleichzeitig betroffene Beobachtungen live beobachtet: SAR, ELVA, CLBK, ADVB, IMNN, CBIO, FIEE, DRIO, BWMX, MKTX, ANIK, XHLD). `layer6_sell_signal.py` (Frühsignal-Gegensignale) hatte die `watch=false`-Beschränkung bereits (Josefs frühere Klarstellung „nur für bestätigte Käufe, nicht für alle Empfehlungen") – jetzt konsistent auch für den Sentiment-Check: der komplette Sell-Signal-Block in `_run_portfolio_scan_inner()` steht jetzt unter `if not entry.get("watch"):`. Bereits fälschlich gesetzte `sell_signal`-Flags auf den 12 betroffenen Beobachtungen wurden einmalig live bereinigt.

## Portfolio-Sortierung + „Position eintragen"-Button beim Tagespick (2026-08-14)

Josef-Feedback direkt nach dem Owner-Bugfix (s.o.): (1) Nach dem Hinzufügen fand er seine echte Position zwischen den vielen Auto-Watch-Beobachtungen nicht wieder. (2) Wunsch nach einem direkten Weg, einen Tages-Pick zur echten Position zu machen, statt ihn manuell im Portfolio-Tab zu suchen/anzulegen.

- **Sortierung:** `loadPortfolio()` in `pwa/index.html` sortiert die geladene Liste jetzt stabil nach `watch` (`false` vor `true`), bevor sie gerendert wird – echte Positionen stehen immer oben, Reihenfolge innerhalb der beiden Gruppen bleibt wie von der API geliefert.
- **„Position eintragen"-Button** unter jeder Tagespick-Karte (`pickMakeReal(ticker, price_at_pick)`, `dash-pick-content`): lädt den aktuellen Portfolio-Stand und verzweigt clientseitig – Ticker bereits `watch:true` → ~~ruft den bestehenden `convertToReal()`-Prompt-Dialog (PATCH `.../convert`) auf~~ **seit 2026-08-21: wechselt wie „noch nicht im Portfolio" zum Portfolio-Tab-Formular, siehe unten**; bereits `watch:false` → Hinweis „bereits als echte Position vorhanden"; noch nicht im Portfolio → wechselt per `switchTab('portfolio')` in den Portfolio-Tab und befüllt `#pf-ticker`/`#pf-price` vor (Stückzahl/Datum/Währung kennt der Pick nicht, kein Auto-Submit). Kein neuer Endpoint nötig – reine Frontend-Verzweigung auf bereits vorhandenen APIs.

## Tab-Leiste an unteren Bildschirmrand verschoben (2026-08-14, v1.21)

Josef-Wunsch: Tab-Leiste (Dashboard/Portfolio/Früh/Analyse) smartphonegerecht ans untere Bildschirmende, wie bei nativen Apps – Referenz-Implementierung für den neuen PKA-Standard `PKA/BKM/PWA-Standards.md` „Tab-Leiste am unteren Bildschirmrand" (Variante A, feste Tab-Anzahl).

- `<nav class="tabs">` aus dem `<header>` gelöst, jetzt direkt nach `</main>` im DOM, `position:fixed;bottom:0`. Icons in den Tab-Buttons von 14px auf 20px vergrößert (besser lesbar am unteren Rand), Icon jetzt über statt neben dem Label (`flex-direction:column`).
- `header`: unteres Padding wieder auf `14px` gesetzt (vorher lieferte die eingebettete `.tabs`-Leiste den Abstand).
- `main`: `padding-bottom` von `32px` auf `72px` (+ safe-area) erhöht, damit Inhalt nicht hinter der fixierten Leiste verschwindet.
- `.back-top`: `bottom`-Wert von `24px` auf `72px` (+ safe-area) angehoben, sonst Überlappung mit der neuen Tab-Leiste.
- `switchTab()`/Button-IDs/Onclick-Handler unverändert – reine Positions-/Style-Änderung, keine Logikänderung.

## Position schließen + Dashboard-Eintrag vereinheitlicht + Telegram-Härtung (2026-08-21, v1.23, ADR-018)

Josef-Feedback (4 zusammenhängende Punkte): (1) Positionen ließen sich nur löschen, nicht schließen – ein Schließen soll die Position wie eToros "Geschichte des Trades" weiter sichtbar halten. (2) Die EMBC-Verkaufsempfehlung kam nicht per Telegram an. (3) Der Dashboard-Weg ("Position eintragen" beim Tages-Pick) führte zu einer unschönen `prompt()`-Eingabemaske statt des Portfolio-Tab-Formulars. (4) Nach erfolgreichem Eintragen blieb die Tages-Pick-Card unverändert stehen ("Position eintragen"), was einen Fehlschlag vortäuschte.

- **`closed`-Feld** (neu, `portfolio.json`-Eintrag: `closed`, `close_price`, `close_date`, `realized_pnl`, `realized_pnl_pct` – siehe ADR-018): `PATCH /api/portfolio/<ticker>` mit `{"closed": true}` schließt eine echte Position (nicht für `watch:true`, nicht doppelt schließbar → 400) zum zuletzt bekannten `current_price`, kein manueller Kurs-/Datums-Prompt. `_run_portfolio_scan_inner()`, `_update_portfolio_quotes()` (`scanner.py`) und `check_frühsignal_sell_exits()` (`layer6_sell_signal.py`) überspringen `closed:true`-Einträge (kein Live-Fetch mehr nötig). PWA: `loadPortfolio()` trennt in offene Karten (`pf-list`) und eine neue Rubrik „📁 Geschlossene Positionen" (`pf-closed-list`, eToro-Stil: Investiert/Einheiten/Open/Ende/G-V). Neuer Button „Schließen" neben „Löschen" bei echten, offenen Positionen (`closeFromPortfolio()`).
- **Dashboard-Eintrag vereinheitlicht:** Die alte `convertToReal()`-Funktion (4× `prompt()` für Anzahl/Währung/Kurs/Datum) ist komplett entfernt. `addToPortfolio()` (das "Aktie hinzufügen"-Formular im Portfolio-Tab) erkennt jetzt selbst bei `409 Ticker bereits im Portfolio`, ob der bestehende Eintrag eine `watch:true`-Beobachtung ist – falls ja, ruft es mit denselben Formulardaten `PATCH .../convert` auf (Umwandlung), statt eine Sackgassen-Fehlermeldung zu zeigen. `pickMakeReal()` (Tages-Pick-Button) und der „Zu echter Position machen"-Button in der Portfolio-Liste (`prefillConvert()`) befüllen beide nur noch das Formular und wechseln in den Portfolio-Tab – keine eigene Eingabemaske mehr, ein einheitlicher Flow für alle drei Einstiegspunkte.
- **Dashboard-Refresh-Bug behoben:** `addToPortfolio()` ruft nach Erfolg jetzt zusätzlich `loadDailyPick()` auf (vorher nur `loadPortfolio()`) – die Tages-Pick-Card zeigte sonst nach erfolgreichem Eintragen weiter „Position eintragen" an, obwohl die Position schon im Portfolio war (live beobachtet beim IMNM-Eintrag 2026-08-21).
- **Telegram-Härtung (`_tg_post()`, `scanner.py`):** Bounded Retry (3 Versuche, Backoff) bei Timeout/Connection-Error/HTTP 429/5xx – vorher ging ein Alert bei jedem einmaligen Netzwerk-Hänger endgültig verloren, ohne zweite Chance (mutmaßliche Ursache für den fehlenden EMBC-Alert vom 2026-08-20, nicht abschließend im Server-Log bestätigt). Zusätzlich `_sanitize_tg_error()`: entfernt das Bot-Token aus geloggten Exception-Texten (manche `requests`-Fehler betten die volle Request-URL inkl. Token ein) – Sicherheits-Härtung, damit das Token nie im Klartext in `scan.log` landen kann.

## Portfolio-Suche + kritischer Merge-Bug behoben (2026-08-21, v1.24)

Josef bat um eine Suche im Portfolio-Tab (viele Einträge, eine bestehende Position beim Umwandeln nicht wiederauffindbar) und meldete dabei, dass seine IMNM-Position „nicht mehr im Portfolio" sei, obwohl er sie zuvor erfolgreich in eine echte Position umgewandelt hatte.

- **Root Cause gefunden – echter Datenverlust-Bug, nicht Bedienfehler:** `_merge_portfolio_updates()` (`scanner.py`) ersetzte bisher den KOMPLETTEN Portfolio-Eintrag eines Tickers durch die Scan-Momentaufnahme vom Lauf-Anfang. Wandelte Josef eine Position waehrend eines laufenden Portfolio-Scans manuell um (watch→real, `shares`/`buy_price`/`buy_date` aendern sich), schrieb der Scan am Ende seine veraltete, noch unkonvertierte Version zurueck und ueberschrieb die Umwandlung spurlos – sichtbar als „vorhin bestaetigte Aenderung ist wieder weg". Live reproduziert: IMNM fiel exakt auf dieses Muster zurueck (watch:true, shares:1, alter Alert-Kurs, altes Alert-Datum vom 19.08., obwohl Josef sie am 20./21.08. bereits mit 11 Aktien / $28.00 konvertiert hatte).
- **Fix:** `_merge_portfolio_updates()` uebernimmt jetzt nur noch die tatsaechlich vom Scan berechneten Felder (`_SCAN_OWNED_FIELDS`: `current_price`, `current_value`, `pnl`, `pnl_pct`, `price_stale`, `price_updated_at`, `last_sentiment`, `sell_signal`, `sell_reason`, `sell_signal_source`) auf den AKTUELLEN Eintrag, statt ihn komplett zu ersetzen. Strukturelle Felder (`ticker`/`shares`/`buy_price`/`buy_date`/`watch`/`closed`/`close_price`/...) kommen dadurch immer aus der aktuellen Datei, nie aus dem Scan-Snapshot – schuetzt nebenbei auch die neue „Schließen"-Funktion (v1.23) vor demselben Muster, falls eine Position waehrend eines laufenden Scans geschlossen wird. Mit synthetischen Daten verifiziert (simulierter Scan-Snapshot vs. zwischenzeitliche Konvertierung).
- **IMNM einmalig wiederhergestellt** (Josef-Bestaetigung: 11 Aktien, $28.98, Kaufdatum 21.08.2026 – tatsaechlicher eToro-Auftrag) direkt ueber `scanner._update_portfolio()` auf dem Server.
- **Suche:** Neues Suchfeld ueber der Positionsliste (`#pf-search`, PWA-Standard-Muster mit ✕-Lösch-Button), filtert `loadPortfolio()`/`renderPortfolio()` clientseitig nach Ticker/Name, deckt offene UND geschlossene Positionen ab. Kein neuer Endpoint noetig.

## Fable-Review (2026-08-21, v1.25): 5 Findings, alle gefixt

Unabhängiger Fable-Review über das gesamte Projekt (Josef-Wunsch, allgemeine Sauberkeits-/Sicherheitsprüfung). Alle 5 Findings gegen den echten Code verifiziert, dann gefixt:

- **Merge-Fix-Nachbesserung:** Der am selben Tag gefixte `_merge_portfolio_updates()` (IMNM-Bug) übernahm `current_value`/`pnl`/`pnl_pct` weiterhin blind aus der Scan-Momentaufnahme – wurde eine Position während des Scans konvertiert (shares/buy_price ändern sich), zeigte P&L bis zum nächsten 15-Min-Zyklus die falsche (alte) Stückzahl. Kein Datenverlust mehr wie beim Original-Bug, aber falsche Anzeige. Fix: `_calc_position_value()` neu ausgelagert (aus `_apply_price()`), wird nach dem Merge erneut gegen die aktuellen shares/buy_price angewendet statt den Scan-Wert zu kopieren.
- **Config-Validierungslücke (potenziell schwerwiegend):** `_validate_cfg()` prüfte `early_signals` gar nicht auf Objekt-Typ – ein `POST /api/config` mit `early_signals: null` wurde unbemerkt akzeptiert und persistiert, crashte dann aber erst in `_reschedule()` NACH `scheduler.remove_all_jobs()` – alle Scans (Vollscan, Portfolio-Scan, Frühsignale) wären bis zum nächsten Service-Neustart komplett gestoppt gewesen. Fix: `_validate_cfg()` prüft jetzt zusätzlich `early_signals`/`weekly_analysis` auf Objekt-Typ, analog zum bestehenden `filter`-Check.
- **`insider_sell` im Früh-Tab unsichtbar:** `ES_FILTER_TYPES`/`ES_TYPE_LABEL` wurden beim `insider_sell`-Rollout (2026-08-09, Layer 6) nie ergänzt – das Gegensignal steuerte bereits reale Verkaufssignal-Vetos, war im Signal-Feed aber unsichtbar und nicht filterbar. Ergänzt (Label, Filter-Chip, `esFormatDetail()`-Zweig analog `insider_buy`).
- **Negative Stückzahl/Kurs nicht validiert:** `api_portfolio_add()` prüfte nur auf numerisch, nicht auf `>0` (im Gegensatz zum bereits vorhandenen Check in `api_portfolio_convert()`). Server- und clientseitig ergänzt.
- **Kein Rate-Limit auf `/sentiment/login`:** Einziger Login-Endpoint ohne Drosselung (im Gegensatz zu Claude-Remote/OrgKompass). Neue nginx-Location `= /sentiment/login` mit der bereits vorhandenen, bisher ungenutzten `auth_zone` (5r/m, burst=3) – live verifiziert (429 ab dem 5. Versuch), restlicher `/sentiment/`-Prefix unverändert ungedrosselt.

Alle Fixes lokal syntaxgeprüft, Merge-Fix-Nachbesserung mit synthetischen Daten verifiziert, deployed, nginx-Rate-Limit live gegen die echte URL getestet.

## Median + Trefferquote im Analyse-Tab (2026-08-21, v1.26)

Direkte Konsequenz aus der Performance-Analyse vom selben Tag: der Durchschnitt allein wirkte irreführend positiv (Median Frühsignal-Alerts ~0% vs. Ø +25%, von wenigen Ausreißern verzerrt).

- **`weekly_analysis.py`:** neue Funktion `_overall_stats(rows)` (n, mean_ret_pct, median_ret_pct, hit_rate_pct = Anteil `ret_pct > 0`) – ergänzt in `_analyze_sentiment()`, `_analyze_early_signals()`, `_analyze_cross_signal()` als neues Report-Feld `"overall"`, unabhängig von den bestehenden Pos-/Neg-Schwellenwert-Gruppen (die weiterhin nur den jeweiligen Rand der Verteilung zeigen).
- **PWA:** neue Kachel `anOverallCard()` oberhalb der Positiv-/Negativ-Karten in beiden Analyse-Ansichten (Standard-Report + Cross-Signal), nutzt die bestehenden `pf-grid`/`pf-kpi`-CSS-Klassen (kein neues CSS nötig).
- Mit echten Server-Daten verifiziert (n=92 Frühsignal-Alerts: Median −0,04%, Ø +25,08%, Trefferquote 46,7% – deckt sich mit der manuellen Analyse vom selben Tag).

## Split-bereinigte Renditeberechnung + Rückwirkende Neuberechnung (2026-08-21, v1.27, ADR-019)

`yf_helper.fetch_closes()` nutzte `auto_adjust=False` – ein Aktien-Split (v.a. Reverse Splits bei Sub-$1-Aktien) zwischen Referenz-Tag und Horizont-Tag ließ Renditen um mehrere tausend Prozent zu hoch erscheinen (live gefunden: WETO zeigte „+7.322%", tatsächlich Reverse Split 1:100 am 03.08.2026, korrigierter Wert: **−25,78%**). Details/Begründung: `ADR-019`.

- **Fix (künftig):** `auto_adjust=True` + Referenzkurs immer aus derselben frisch abgerufenen, bereinigten Kursreihe (`forward_tracker.py`/`scan_tracker.py`), nicht mehr aus externer Finnhub-Quote/altem gespeicherten Wert gemischt.
- **Rückwirkend neu berechnet** (`backfill_split_adjusted_returns.py`, einmalig ausgeführt): 1187 `forward_returns` + 5592 `scan_forward_returns`-Werte neu berechnet, 65 davon mit Abweichung ≥20 Prozentpunkte (echte Split-Kontamination).
- **Ergebnis nach Korrektur (deutlich ehrlicher als vorher):** Sentiment-Scan-Empfehlungen: Median **−13,46%**, Trefferquote nur **31,7%** (n=60) – vorher wirkte das durch die Datenfehler positiver. Frühsignal-Alerts: Median **0,0%**, Ø **+0,44%** (vorher fälschlich Ø +25%), Trefferquote 47,8% (n=92) – im Wesentlichen Münzwurf-Niveau.
- Frisch mit `analyze_and_store()` pro System verifiziert (nicht über `run_weekly_analysis()`, um den Telegram-Alert für „neue Erkenntnisse" nicht versehentlich als Nebeneffekt der Verifikation auszulösen).

## tickers.csv erneuern (quartalsweise)

```bash
ssh root@89.167.104.145
cd /opt/sentiment-scanner
venv/bin/python3 fetch_tickers.py
systemctl restart sentiment-scanner
```

## SW-Cache

Name: `sentiment-v1` – bei Änderungen an manifest.json oder sw.js selbst hochzählen.
