# ADR-006: Frühsignal-Layer (EDGAR/Volumen/Buzz/Scoring)

**Datum:** 2026-07-06
**Status:** aktiv
**Projekt:** Stock Sentiment Scanner

## Problem

Der bestehende Scanner reagiert auf News-Buzz – der liegt zeitlich meist nach dem
eigentlichen Kursanstieg. Josef braucht Frühindikatoren, die vor dem News-Buzz
liegen (Insider-Käufe, ungewöhnliches Handelsvolumen, beginnende Buzz-Beschleunigung),
um nicht immer erst auf dem Höchststand zu kaufen.

## Entscheidung

Vier zusätzliche Signal-Layer, alle schreibend in eine gemeinsame SQLite-Tabelle
`signals` (WAL-Modus), orchestriert über die bestehende APScheduler-Instanz in
`app.py`, komplett hinter Feature-Flag `early_signals.enabled` (Default `false`):

1. **Layer 1 – Insider-Käufe:** SEC-EDGAR-Atom-Feed (Form 4), nur Open-Market-Käufe
   (Transaction Code `P`), MarketCap-gefiltert über bestehenden Finnhub-`/stock/metric`-Call.
2. **Layer 2 – Volumen-Anomalie:** z-Score des Tagesvolumens via **yfinance**
   (nicht Finnhub).
3. **Layer 3 – Buzz-Beschleunigung:** aus Tages-News-Counts, die als Hook im
   bestehenden Vollscan mitgeschrieben werden (`buzz_history`) – **keine neuen
   API-Calls**.
4. **Layer 4 – Kombinations-Scoring:** Alert erst ab ≥2 unabhängigen Signaltypen
   und Mindestscore, mit Cooldown und Forward-Return-Tracker zur ehrlichen
   Validierung (1/5/20 Handelstage vs. Kursverlauf).

Alle Scheduler-Jobs laufen mit explizitem `timezone="America/New_York"`
(DST-sicher, unabhängig von der Server-Systemzeit).

## Begründung

- **yfinance statt Finnhub `/stock/candle`:** Der Candle-Endpoint ist im Free
  Tier nicht (mehr) verfügbar (403) – bereits bekanntes Muster in diesem Projekt
  (`/news-sentiment`, Index-Endpoints). yfinance liefert EOD-Volumen für das
  gesamte Ticker-Universum kostenlos und ohne zusätzliches Finnhub-Kontingent.
- **APScheduler statt separatem Cron:** Nutzt bestehende venv, Secrets-Loading,
  Logging und Prozess – kein zweiter Ausführungskontext auf dem Server.
- **SQLite mit WAL statt weiterer JSON-Dateien:** Die Datenmodelle sind
  relational (alerts ↔ forward_returns, Dedup via UNIQUE-Constraints) – dafür
  ist SQLite die richtige Wahl. WAL, da Flask-Thread und Scheduler-Jobs parallel
  zugreifen.
- **MarketCap-Filter für Insider-Signale:** `tickers.csv` ist keine Russell-2000-
  Liste, sondern alle ~4700 US Common Stocks (NYSE+NASDAQ) – ohne Filter würden
  auch Insider-Käufe bei Mega-Caps (geringer Signalwert) alarmieren.
- **Layer 3 ohne neue API-Calls:** Der Vollscan holt die News-Artikel ohnehin
  schon; die Tages-Counts fallen aus den vorhandenen Artikel-Timestamps ab.
- **Feature-Flag statt sofortigem Vollbetrieb:** Reversibilität – jederzeit
  komplett abschaltbar, ohne Code-Änderung.
- **Explizite `timezone` bei jedem neuen Job:** Der Server läuft auf
  Europe/Berlin (nicht UTC) – ohne expliziten Parameter hätten die Jobs in
  Server-Lokalzeit statt Börsenzeit gefeuert (siehe auch der im Zuge dessen
  gefundene und behobene Bug bei den *bestehenden* Vollscan-/Portfolio-Jobs).

## Verworfen

| Alternative | Warum verworfen |
|---|---|
| Finnhub `/stock/candle` für Volumendaten | Nicht im Free Tier (403), bereits mehrfach in diesem Projekt beobachtetes Muster |
| Cron/systemd-Timer statt APScheduler | Zweiter Ausführungskontext, eigene venv/Secrets-Handhabung nötig, Architekturbruch |
| Weitere JSON-Dateien statt SQLite | Keine sinnvolle relationale Verknüpfung (alerts↔forward_returns) ohne Joins/Queries möglich |
| Social-Media-Layer (Reddit/StockTwits) in v1 | API-Bedingungen unklar, bewusst auf v2 verschoben (siehe `EARLY_SIGNALS.md` Abschnitt 9) |
| Earnings-Kalender-Abgleich für Volumen-Layer in v1 | Finnhub-Free-Tier-Verfügbarkeit von `/calendar/earnings` ungeklärt; falsche Alarme an Earnings-Tagen werden stattdessen über den Forward-Return-Tracker sichtbar gemacht und später entschieden |
| Alle Ticker aus tickers.csv ungefiltert für Insider-Signale | Would alarm on mega-caps with low signal value → MarketCap-Filter ergänzt |

## Gilt unter

- Free-Tier-Grenzen von Finnhub und SEC-EDGAR-Zugriffsregeln bleiben wie zum
  Zeitpunkt der Implementierung (2026-07-06) – bei Änderungen (z. B. SEC
  Rate-Limits) ggf. anpassen.
- yfinance bleibt verfügbar/funktionsfähig (inoffizielle API, kein SLA) –
  bei Ausfall greift Logging + WARNING, kein Hard-Fail des Gesamtscans.
- Kein Alert gilt als Anlageempfehlung; Aussagekraft der Signale wird
  ausschließlich über den eigenen Forward-Return-Tracker beurteilt (siehe
  `EARLY_SIGNALS.md`, Ehrlichkeits-Grundsatz), erst nach 2–3 Monaten Datensammlung.

## Konsequenzen

- Neue Datei `signals.db` (gitignored) auf dem Server, neue Dependency `yfinance`.
- 6 neue Scheduler-Jobs (edgar_scan, volume_scan, buzz_accel, es_scoring,
  es_tracker – alle America/New_York) zusätzlich zu den 2 bestehenden.
- Kein zusätzlicher Finnhub-Verbrauch (Layer 2/3 nutzen andere Quellen bzw.
  Bestandsdaten; Layer 1 nutzt `/stock/metric` nur für tatsächliche Insider-Treffer).
- PWA-Tab „Frühsignale" (Anzeige von Signalen/Alerts/Trefferquote) folgt in
  separater Session – Backend ist unabhängig davon bereits vollständig live.
