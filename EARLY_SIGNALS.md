# EARLY_SIGNALS.md — Frühsignal-Layer für den Sentiment Scanner

**Zweck dieses Dokuments:** Umsetzungsanleitung für Claude Code. Erweiterung des bestehenden
Sentiment Scanners (Hetzner CX23, Ubuntu 24.04, Python/Flask, SQLite, Finnhub Free API,
Telegram-Alerts, Russell-2000-Universum) um Frühindikatoren, die zeitlich **vor** dem
News-Buzz liegen.

**Ehrlichkeits-Grundsatz (nicht löschen):** Kein Layer in diesem Dokument garantiert einen
früheren Einstieg. Ob die Signale prädiktiv sind, ist erst nach eigenem Forward-Return-Tracking
(Abschnitt 7) beurteilbar. Dieses Dokument ist Methodik, keine Anlageberatung.

---

## 1. Architektur-Übersicht

```
                    ┌─────────────────────────────┐
                    │  Cron-Jobs (systemd timer    │
                    │  oder crontab)               │
                    └──────────┬──────────────────┘
                               │
   ┌───────────────┬───────────┼────────────────┐
   ▼               ▼           ▼                ▼
Layer 1         Layer 2     Layer 3         (bestehend)
EDGAR Form 4    Volumen-    Buzz-           Finnhub
Insider-Käufe   Anomalie    Beschleunigung  Sentiment/News
   │               │           │                │
   └───────┬───────┴─────┬─────┴────────┬───────┘
           ▼             ▼              ▼
        SQLite: signals (einheitliches Schema, Abschnitt 6)
           │
           ▼
Layer 4: Kombinations-Scoring (Cron, alle 15 Min.)
           │
           ├──► Telegram-Alert (nur bei ≥2 unabhängigen Frühsignalen)
           └──► Flask PWA Dashboard (neuer Tab "Frühsignale")

Parallel: Forward-Return-Tracker (täglich, Abschnitt 7)
```

Designprinzip: Jeder Layer schreibt in **dieselbe** `signals`-Tabelle mit Zeitstempel.
Das Scoring liest nur aus dieser Tabelle. So bleibt jeder Layer einzeln testbar und
abschaltbar.

---

## 2. Layer 1 — Insider-Käufe (SEC EDGAR Form 4)

### 2.1 Rechtlicher/zeitlicher Hintergrund (verifiziert)

- Insider (Directors, Officers, >10%-Eigner) müssen Transaktionen binnen
  **2 Geschäftstagen** nach dem Transaktionsdatum per Form 4 melden
  (Sarbanes-Oxley Act 2002, Section 403; 15 U.S.C. § 78p(a)).
- Section-16-Filings (Forms 3, 4, 5) können bis 22:00 Uhr ET eingereicht werden und
  erhalten noch das Filing-Datum desselben Tages; EDGAR disseminiert sie am selben Tag.
  Quelle: SEC.gov, "Submit Filings" bzw. Filer-Support-Seiten (sec.gov/submit-filings).
- Konsequenz: Form 4 ist die schnellste **kostenlose, offizielle** Quelle in diesem System.
  Realer Zeitverzug zum Insider-Trade: 0–2 Geschäftstage.

### 2.2 Datenzugriff

**Primärquelle — EDGAR "Latest Filings" Atom-Feed:**

```
https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=4&company=&dateb=&owner=include&count=100&output=atom
```

- Liefert die neuesten Form-4-Filings als Atom/XML.
- **Pflicht:** HTTP-Header `User-Agent` mit Name + E-Mail setzen (SEC-Vorgabe für
  automatisierte Zugriffe). Ohne diesen Header blockt die SEC.
- Rate-Limit der SEC laut Webmaster-FAQ: max. 10 Requests/Sekunde.
  ⚠️ ZU VERIFIZIEREN vor Implementierung: aktuelle Fassung unter
  https://www.sec.gov/os/webmaster-faq#developers prüfen (Bedingungen können sich ändern).

**Sekundärquelle (Backfill/Robustheit) — Daily Index:**

```
https://www.sec.gov/Archives/edgar/daily-index/
```

### 2.3 Parsing-Logik

1. Atom-Feed alle 10–15 Min. abrufen (Cron). Neue Filing-URLs gegen SQLite-Tabelle
   `edgar_seen` deduplizieren (Accession Number als Primary Key).
2. Für jedes neue Filing das Form-4-**XML** laden (nicht das HTML) — Dateiname endet
   typischerweise auf `.xml` im Filing-Index.
3. Relevante Felder:
   - `issuerTradingSymbol` → Abgleich mit Russell-2000-Universum (bestehende Ticker-Liste).
   - `transactionCode` — **nur Code `P`** (Open-Market-Kauf) ist ein Kaufsignal.
     Code `S` = Verkauf, `A` = Award/Zuteilung (kein Kaufsignal!), `M` = Optionsausübung.
   - `transactionShares`, `transactionPricePerShare` → Transaktionsvolumen in USD.
   - `isDirector` / `isOfficer` / `officerTitle` → Gewichtung (CEO/CFO-Käufe höher gewichten).
4. Filter (Startwerte, per Backtest justieren):
   - Transaktionswert ≥ 25.000 USD (filtert Kleinstkäufe).
   - Cluster-Bonus: ≥2 verschiedene Insider desselben Tickers innerhalb 5 Handelstagen.
5. Schreiben in `signals` mit `signal_type = 'insider_buy'`.

---

## 3. Layer 2 — Volumen-Anomalie ohne News

### 3.1 Idee

Ungewöhnliches Handelsvolumen bei gleichzeitig **flachem** News-Count läuft dem
öffentlichen Sentiment zeitlich voraus. Reine Statistik, vollständig aus eigenen Daten
nachrechenbar.

### 3.2 Datenquelle

⚠️ ZU VERIFIZIEREN: Ob der Finnhub-Free-Tier aktuell den Candle-Endpoint
(`/stock/candle`) für US-Aktien enthält, ist unsicher — die Free-Tier-Abdeckung hat
sich in der Vergangenheit geändert. Vor Implementierung prüfen: https://finnhub.io/pricing

**Fallback, falls Finnhub Free keine Candles liefert:** `yfinance` (Python-Paket,
inoffizielle Yahoo-Finance-Schnittstelle). Hinweis auf Risiko: inoffizielle API,
kann jederzeit brechen; für tägliche EOD-Volumendaten in der Praxis verbreitet,
aber ohne SLA. Bei Ausfall: Log-Eintrag + Telegram-Warnung (bestehendes
Notification-Muster des Servers wiederverwenden).

Benötigt wird nur **1 Request pro Ticker pro Tag** (EOD-Daten nach US-Börsenschluss,
d.h. Cron ~22:30 Uhr ET / 04:30 Uhr MEZ — Zeitzone im Cron beachten, Server läuft
vermutlich auf UTC).

### 3.3 Berechnung (Rechenweg)

Für jeden Ticker `t` am Tag `d`:

```
mean_vol(t)  = Mittelwert(Volumen der letzten 20 Handelstage, ohne Tag d)
sd_vol(t)    = Standardabweichung derselben 20 Tage
z_score(t,d) = (Volumen(t,d) − mean_vol(t)) / sd_vol(t)
```

Signalbedingung (Startwerte):

```
z_score ≥ 2.5
UND news_count_letzte_3_Tage(t) ≤ Median des Tickers   ← "ohne News"-Bedingung
```

Randfälle behandeln:
- `sd_vol == 0` (illiquide Titel) → Signal überspringen.
- Weniger als 20 Tage Historie → überspringen, erst Historie aufbauen.
- Bekannte Termine (Earnings) erzeugen erwartbare Volumenspitzen — wenn das
  Earnings-Datum verfügbar ist (Finnhub-Endpoint `/calendar/earnings`,
  ⚠️ Free-Tier-Verfügbarkeit ebenfalls prüfen), diese Tage markieren statt alarmieren.

Schreiben in `signals` mit `signal_type = 'volume_anomaly'`, `score = z_score`.

---

## 4. Layer 3 — Buzz-Beschleunigung (bestehende Daten)

Voraussetzung: News-Counts pro Ticker werden **historisiert** (falls der Scanner bisher
nur den aktuellen Stand hält: neue Tabelle `buzz_history(ticker, date, news_count,
bullish_pct)` und täglich befüllen).

Berechnung:

```
accel(t,d) = news_count(t, d−0..d−2) − news_count(t, d−3..d−5)   # 3-Tage-Fenster-Differenz
rel_accel  = accel / max(1, news_count(t, d−3..d−5))              # relative Beschleunigung
```

Signalbedingung (Startwert): `rel_accel ≥ 1.0` (Verdopplung) UND absoluter Count noch
unter dem bestehenden Buzz-Schwellwert des Scanners — d.h. das Signal feuert bewusst
**bevor** der bestehende Filter anschlägt.

`signal_type = 'buzz_accel'`.

---

## 5. Layer 4 — Kombinations-Scoring & Alerts

Cron alle 15 Min. (bzw. 1× täglich nach dem EOD-Lauf für Layer 2/3):

1. Alle Signale der letzten 5 Handelstage je Ticker aggregieren.
2. **Alert-Bedingung: ≥2 unterschiedliche `signal_type` für denselben Ticker.**
   Begründung: Ein einzelnes Frühsignal hat hohe False-Positive-Rate; die Koinzidenz
   unabhängiger Quellen ist das eigentliche Signal.
3. Gewichtung (Startwerte, bewusst simpel — erst nach Validierung verfeinern):
   - insider_buy (Officer/Director, ≥25k USD): 3 Punkte, Cluster: +2
   - volume_anomaly (z ≥ 2.5): 2 Punkte; z ≥ 4: 3 Punkte
   - buzz_accel: 1 Punkt
   - Alert ab ≥4 Punkten aus ≥2 Typen.
4. Telegram-Nachricht im bestehenden Format, ergänzt um: beteiligte Signale mit
   Zeitstempeln + Link zum EDGAR-Filing (Transparenz/Nachprüfbarkeit).
5. Jeden Alert in `alerts`-Tabelle loggen (für Abschnitt 7).

**Anti-Spam:** pro Ticker max. 1 Alert / 5 Handelstage (Cooldown-Spalte).

---

## 6. SQLite-Schemata

```sql
CREATE TABLE IF NOT EXISTS signals (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker        TEXT NOT NULL,
    signal_type   TEXT NOT NULL,      -- 'insider_buy' | 'volume_anomaly' | 'buzz_accel'
    signal_ts     TEXT NOT NULL,      -- ISO 8601, UTC
    score         REAL,
    details_json  TEXT,               -- Rohdaten: z-Score, Insider-Name, Filing-URL etc.
    UNIQUE(ticker, signal_type, signal_ts)
);

CREATE TABLE IF NOT EXISTS edgar_seen (
    accession_no  TEXT PRIMARY KEY,
    seen_ts       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS buzz_history (
    ticker        TEXT NOT NULL,
    date          TEXT NOT NULL,
    news_count    INTEGER,
    bullish_pct   REAL,
    PRIMARY KEY (ticker, date)
);

CREATE TABLE IF NOT EXISTS alerts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker        TEXT NOT NULL,
    alert_ts      TEXT NOT NULL,
    total_score   REAL,
    signal_ids    TEXT,               -- JSON-Array der beteiligten signals.id
    price_at_alert REAL               -- Schlusskurs/letzter Kurs zum Alertzeitpunkt
);

CREATE TABLE IF NOT EXISTS forward_returns (
    alert_id      INTEGER NOT NULL REFERENCES alerts(id),
    horizon_days  INTEGER NOT NULL,   -- 1 | 5 | 20
    ret_pct       REAL,               -- (Kurs_horizon / price_at_alert − 1) * 100
    filled_ts     TEXT,
    PRIMARY KEY (alert_id, horizon_days)
);
```

---

## 7. Validierung: Forward-Return-Tracker (wichtigster Baustein)

Täglicher Cron nach EOD:

1. Offene Zeilen in `forward_returns` suchen, deren Horizont (1/5/20 Handelstage
   seit `alert_ts`) erreicht ist.
2. Schlusskurs holen, `ret_pct` berechnen, füllen.
3. Monatliche Auswertung (einfaches Script oder PWA-Tab):
   - Trefferquote (ret_pct > 0) je `signal_type`-Kombination und Horizont.
   - Durchschnittsrendite vs. Benchmark (z.B. IWM-ETF als Russell-2000-Proxy)
     im selben Zeitraum — ohne Benchmark-Vergleich ist die Trefferquote wertlos.
4. **Entscheidungsregel:** Erst nach ≥2–3 Monaten Datensammlung Schwellwerte/Gewichte
   anpassen. Keine Anpassung nach Einzelfällen (Overfitting-Gefahr).

---

## 8. Cron-Plan (Vorschlag, Zeiten in UTC)

```
*/15 13-22 * * 1-5   layer1_edgar_form4.py      # US-Handelstage, alle 15 Min.
30 21 * * 1-5        layer2_volume_anomaly.py    # nach US-Börsenschluss (20:00 UTC Sommer / 21:00 Winter — DST beachten!)
35 21 * * 1-5        layer3_buzz_accel.py
40 21 * * 1-5        layer4_scoring_alerts.py
45 21 * * 1-5        forward_return_tracker.py
```

⚠️ US-Sommerzeit (EDT/EST) verschiebt Börsenschluss in UTC. Entweder Cron-Zeiten
konservativ spät setzen oder im Script gegen `zoneinfo("America/New_York")` prüfen.

---

## 9. Offene Punkte — vor Implementierung verifizieren

| # | Punkt | Wo prüfen |
|---|-------|-----------|
| 1 | Finnhub Free: `/stock/candle` und `/calendar/earnings` enthalten? Rate-Limits? | https://finnhub.io/pricing und /docs |
| 2 | SEC-Zugriffsregeln (User-Agent, Rate-Limit) aktuelle Fassung | https://www.sec.gov/os/webmaster-faq#developers |
| 3 | Struktur des Form-4-XML an 2–3 echten Filings testen, bevor der Parser produktiv geht | Beliebiges Form 4 via EDGAR öffnen |
| 4 | Russell-2000-Tickerliste: CIK-Mapping nötig (EDGAR arbeitet mit CIK, nicht Ticker) — Mapping-Datei: https://www.sec.gov/files/company_tickers.json | SEC.gov |
| 5 | Social-Layer (Reddit/StockTwits) bewusst NICHT in v1 — API-Bedingungen unklar, erst separat prüfen | — |

---

## 10. Quellen

- Sarbanes-Oxley Act 2002, Sec. 403 (Form-4-Frist von 2 Geschäftstagen):
  15 U.S.C. § 78p(a); SEC Final Rule 34-46421 (sec.gov/rules/final/34-46421.htm)
- EDGAR-Betriebszeiten und Same-Day-Dissemination für Forms 3/4/5 bis 22:00 ET:
  SEC.gov, Submit Filings / Filer Support (sec.gov/submit-filings), abgerufen Juli 2026
- EDGAR-Programmzugriff, User-Agent-Pflicht, Rate-Limits:
  SEC Webmaster FAQ (sec.gov/os/webmaster-faq#developers) — vor Nutzung erneut prüfen
- CIK↔Ticker-Mapping: sec.gov/files/company_tickers.json

**Nicht belegt / bewusst offen:** Es wird keine Aussage darüber getroffen, dass diese
Signale zuverlässig prädiktiv sind. Die empirische Studienlage zu Insider-Käufen und
Volumenanomalien bei Small Caps ist gemischt; die Validierung erfolgt ausschließlich
über den eigenen Forward-Return-Tracker (Abschnitt 7).
