# EARLY_SIGNALS_UMSETZUNG.md — Implementierungs-Spec für Claude Code

**Zielgruppe:** Ein Modell, das diese Spec Schritt für Schritt umsetzt, ohne eigene
Architekturentscheidungen treffen zu müssen. Alle Entscheidungen sind hier getroffen.
Hintergrund/Begründungen: siehe `EARLY_SIGNALS.md` (nur lesen, nicht als Bauplan verwenden —
diese Datei hier hat Vorrang, sie enthält die mit Josef abgestimmten Abweichungen).

**Vor Beginn lesen (Pflicht):**
1. `CLAUDE.md` dieses Projekts (Pitfalls-Abschnitt komplett!)
2. Vor Phase C zusätzlich: `~/Dropbox/Apps/Claude/PKA/BKM/PWA-Standards.md` und
   `~/Dropbox/Apps/Claude/PKA/BKM/App-Versionierung.md`

---

## 0. Verbindliche Entscheidungen (Abweichungen von EARLY_SIGNALS.md)

| # | Entscheidung | Begründung |
|---|--------------|------------|
| 1 | **Kein Cron/crontab.** Alle Jobs als APScheduler-Jobs in `app.py` → `_reschedule()` | Bestehende Architektur; gleiche venv, Secrets, Logging |
| 2 | **Layer 2 nutzt yfinance, NICHT Finnhub `/stock/candle`** | Candle-Endpoint ist nicht im Free Tier (403) |
| 3 | **EOD-Jobs laufen mit `timezone="America/New_York"`** | Löst das DST-Problem ohne UTC-Rechnerei |
| 4 | **Layer 3 braucht keine neuen API-Calls** — Tages-News-Counts fallen im bestehenden Vollscan ab (Artikel-Timestamps aus `/company-news`) | Vollscan läuft 2×/Tag (13:00, 19:30 UTC), Daten sind da |
| 5 | **Layer 1 filtert per MarketCap** über bestehenden `/stock/metric`-Call (nur für Treffer, wenige Calls/Tag) | tickers.csv ist NICHT Russell 2000, sondern alle ~4700 US Common Stocks |
| 6 | **SQLite mit WAL-Modus**, Datei `signals.db` im Projektroot, gitignored | Flask-Thread + Scheduler-Jobs greifen parallel zu |
| 7 | **Feature-Flag `early_signals.enabled`** in config.json (Default `false`) | Komplettabschaltung jederzeit möglich, Reversibilität |
| 8 | **Phasen A → B → C strikt nacheinander**, jede Phase einzeln deployen + testen | Fehlereingrenzung |

## 0b. NICHT tun (harte Verbote)

- ❌ NIEMALS `/etc/pka/secrets.env` lesen, sourcen, ausgeben — in keiner Form (globale Regel).
- ❌ KEIN Finnhub `/stock/candle` und KEIN `/news-sentiment` aufrufen (403 im Free Tier).
- ❌ `transactionCode = "A"` ist KEIN Kauf (Award/Zuteilung). Nur Code `"P"` zählt.
- ❌ `marketCapitalization` von Finnhub ist in **Millionen USD** — nie direkt mit USD-Beträgen vergleichen.
- ❌ Keine zusätzlichen Finnhub-Calls in Schleifen über alle 4700 Ticker (Tageslimit!).
- ❌ SEC-Requests ohne `User-Agent`-Header (wird geblockt) oder schneller als 10 req/s.
- ❌ Keine Secrets, Kaufpreise oder personenbezogene Daten in git committen.
- ❌ `signals.db`, `config.json` nicht committen (gitignore, siehe 1.4).

---

## 1. Phase A — DB-Modul, Layer 1 (EDGAR Insider), Layer 3 (Buzz-Historie)

### 1.1 Neue Datei `signals_db.py`

Vollständig so anlegen:

```python
"""SQLite-Layer für Frühsignale. Alle Zugriffe auf signals.db laufen hier durch."""
import json
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "signals.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker        TEXT NOT NULL,
    signal_type   TEXT NOT NULL,      -- 'insider_buy' | 'volume_anomaly' | 'buzz_accel'
    signal_ts     TEXT NOT NULL,      -- ISO 8601, UTC
    score         REAL,
    details_json  TEXT,
    UNIQUE(ticker, signal_type, signal_ts)
);
CREATE INDEX IF NOT EXISTS idx_signals_ticker_ts ON signals(ticker, signal_ts);

CREATE TABLE IF NOT EXISTS edgar_seen (
    accession_no  TEXT PRIMARY KEY,
    seen_ts       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS buzz_history (
    ticker        TEXT NOT NULL,
    date          TEXT NOT NULL,      -- YYYY-MM-DD (Datum des Artikels, nicht des Scans)
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
    price_at_alert REAL
);

CREATE TABLE IF NOT EXISTS forward_returns (
    alert_id      INTEGER NOT NULL REFERENCES alerts(id),
    horizon_days  INTEGER NOT NULL,   -- 1 | 5 | 20 (Handelstage)
    ret_pct       REAL,
    filled_ts     TEXT,
    PRIMARY KEY (alert_id, horizon_days)
);
"""


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_conn() as conn:
        conn.executescript(_SCHEMA)


def insert_signal(ticker: str, signal_type: str, signal_ts: str,
                  score: float, details: dict) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO signals (ticker, signal_type, signal_ts, score, details_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (ticker, signal_type, signal_ts, score, json.dumps(details)),
        )


def upsert_buzz_rows(rows: list[tuple]) -> None:
    """rows: [(ticker, date, news_count, bullish_pct), ...]"""
    with get_conn() as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO buzz_history (ticker, date, news_count, bullish_pct) "
            "VALUES (?, ?, ?, ?)",
            rows,
        )
```

`init_db()` wird in `app.py` beim Start einmal aufgerufen (nach `scheduler = BackgroundScheduler()`,
vor `scheduler.start()`): `from signals_db import init_db; init_db()`.

### 1.2 Neue Datei `layer1_edgar.py`

Aufgabe: EDGAR-Atom-Feed für Form 4 pollen, Open-Market-Käufe (`P`) im eigenen
Ticker-Universum finden, in `signals` schreiben.

```python
"""Layer 1: SEC EDGAR Form 4 — Insider-Open-Market-Käufe."""
import json
import logging
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import requests

from signals_db import get_conn, insert_signal

log = logging.getLogger("sentiment-scanner")

SEC_HEADERS = {"User-Agent": "Josef Fischer josef.jf.fischer@me.com"}
FEED_URL = ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent"
            "&type=4&company=&dateb=&owner=include&count=100&start={start}&output=atom")
ATOM_NS = {"a": "http://www.w3.org/2005/Atom"}
MAX_PAGES = 5          # max 500 Filings pro Lauf
REQ_DELAY = 0.15       # SEC-Limit 10 req/s → konservativ ~6/s


def _sec_get(url: str) -> requests.Response:
    time.sleep(REQ_DELAY)
    r = requests.get(url, headers=SEC_HEADERS, timeout=30)
    r.raise_for_status()
    return r


def _load_universe() -> set[str]:
    from scanner import _load_tickers
    return {t["symbol"].upper() for t in _load_tickers()}


def _feed_entries(start: int) -> list[dict]:
    """Ein Feed-Seite laden. Rückgabe: [{'accession': ..., 'index_url': ...}]"""
    root = ET.fromstring(_sec_get(FEED_URL.format(start=start)).content)
    out = []
    for entry in root.findall("a:entry", ATOM_NS):
        link = entry.find("a:link", ATOM_NS).get("href")
        # id endet auf 'accession-number=0001234567-26-000123'
        raw_id = entry.find("a:id", ATOM_NS).text or ""
        acc = raw_id.rsplit("=", 1)[-1]
        out.append({"accession": acc, "index_url": link})
    return out


def _fetch_form4_xml(index_url: str) -> ET.Element | None:
    """Vom Filing-Index das Form-4-XML finden und parsen."""
    dir_url = index_url.rsplit("/", 1)[0]
    listing = _sec_get(dir_url + "/index.json").json()
    for item in listing.get("directory", {}).get("item", []):
        name = item.get("name", "")
        if not name.lower().endswith(".xml"):
            continue
        try:
            root = ET.fromstring(_sec_get(f"{dir_url}/{name}").content)
        except ET.ParseError:
            continue
        if root.tag == "ownershipDocument":
            return root
    return None


def _txt(el, path, default=""):
    found = el.find(path)
    return (found.text or default).strip() if found is not None and found.text else default


def _parse_form4(root: ET.Element) -> dict | None:
    """Extrahiert Kauf-Transaktionen (Code P). None wenn kein Kauf enthalten."""
    symbol = _txt(root, ".//issuer/issuerTradingSymbol").upper()
    if not symbol:
        return None
    owner = _txt(root, ".//reportingOwner/reportingOwnerId/rptOwnerName")
    is_director = _txt(root, ".//reportingOwnerRelationship/isDirector") in ("1", "true")
    is_officer = _txt(root, ".//reportingOwnerRelationship/isOfficer") in ("1", "true")
    officer_title = _txt(root, ".//reportingOwnerRelationship/officerTitle")

    total_usd = 0.0
    total_shares = 0.0
    for tx in root.findall(".//nonDerivativeTable/nonDerivativeTransaction"):
        code = _txt(tx, ".//transactionCoding/transactionCode")
        acq = _txt(tx, ".//transactionAmounts/transactionAcquiredDisposedCode/value")
        if code != "P" or acq != "A":
            continue
        try:
            shares = float(_txt(tx, ".//transactionAmounts/transactionShares/value", "0") or 0)
            price = float(_txt(tx, ".//transactionAmounts/transactionPricePerShare/value", "0") or 0)
        except ValueError:
            continue
        total_shares += shares
        total_usd += shares * price

    if total_usd <= 0:
        return None
    return {
        "symbol": symbol, "owner": owner, "is_director": is_director,
        "is_officer": is_officer, "officer_title": officer_title,
        "total_usd": round(total_usd, 2), "total_shares": total_shares,
    }


def _market_cap_ok(symbol: str, cfg: dict) -> bool:
    """MarketCap-Filter über Finnhub /stock/metric. Achtung: Wert in MILLIONEN USD."""
    from scanner import _fh_get
    try:
        metric = _fh_get("/stock/metric", {"symbol": symbol, "metric": "all"})
        cap_mio = (metric.get("metric") or {}).get("marketCapitalization")
        if cap_mio is None:
            return True  # keine Daten → nicht wegfiltern, aber im Signal vermerken
        cap_usd = cap_mio * 1_000_000
        f = cfg.get("filter", {})
        return f.get("market_cap_min_usd", 0) <= cap_usd <= f.get("market_cap_max_usd", 10**13)
    except Exception as e:
        log.warning("EDGAR %s marketcap: %s", symbol, e)
        return True


def run_edgar_scan(cfg: dict) -> None:
    es_cfg = cfg.get("early_signals", {})
    min_usd = es_cfg.get("insider_min_usd", 25000)
    universe = _load_universe()
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    new_count, hit_count = 0, 0

    with get_conn() as conn:
        seen = {r["accession_no"] for r in conn.execute("SELECT accession_no FROM edgar_seen")}

    for page in range(MAX_PAGES):
        try:
            entries = _feed_entries(page * 100)
        except Exception as e:
            log.warning("EDGAR Feed Seite %d: %s", page, e)
            break
        if not entries:
            break
        page_all_seen = all(e["accession"] in seen for e in entries)

        for e in entries:
            if e["accession"] in seen:
                continue
            seen.add(e["accession"])
            new_count += 1
            with get_conn() as conn:
                conn.execute("INSERT OR IGNORE INTO edgar_seen VALUES (?, ?)",
                             (e["accession"], now_iso))
            try:
                root = _fetch_form4_xml(e["index_url"])
                if root is None:
                    continue
                buy = _parse_form4(root)
            except Exception as exc:
                log.warning("EDGAR %s: %s", e["accession"], exc)
                continue
            if buy is None or buy["symbol"] not in universe or buy["total_usd"] < min_usd:
                continue
            if not _market_cap_ok(buy["symbol"], cfg):
                continue

            # Cluster-Check: ≥2 verschiedene Insider desselben Tickers in 7 Kalendertagen
            with get_conn() as conn:
                others = conn.execute(
                    "SELECT details_json FROM signals WHERE ticker=? AND signal_type='insider_buy' "
                    "AND signal_ts >= datetime('now', '-7 days')", (buy["symbol"],)).fetchall()
            other_owners = {json.loads(r["details_json"]).get("owner") for r in others}
            cluster = len(other_owners - {buy["owner"]}) >= 1

            score = 3.0 + (2.0 if cluster else 0.0)
            details = dict(buy)
            details["cluster"] = cluster
            details["filing_url"] = e["index_url"]
            insert_signal(buy["symbol"], "insider_buy", now_iso, score, details)
            hit_count += 1
            log.info("EDGAR Insider-Kauf: %s %s %.0f USD (cluster=%s)",
                     buy["symbol"], buy["owner"], buy["total_usd"], cluster)

        if page_all_seen:
            break

    log.info("EDGAR-Lauf fertig: %d neue Filings, %d Kaufsignale", new_count, hit_count)
```

**Hinweise für die Umsetzung:**
- Die XPath-Pfade an 2–3 echten Form-4-XMLs verifizieren, BEVOR der Job aktiviert wird
  (Test siehe 1.6). Feldnamen können minimal abweichen (`value`-Wrapper bei manchen Feldern).
- Der Feed enthält pro Filing auch Duplikate (Issuer- und Owner-Eintrag können getrennt
  gelistet sein) — Dedup über `accession` fängt das ab.
- Bei HTTP 403/429 von der SEC: Lauf abbrechen (kein Retry-Loop), WARNING loggen.

### 1.3 Layer 3 Teil 1 — Buzz-Historie im bestehenden Scan mitschreiben

**Änderung 1 — `scanner.py`, Funktion `_fetch_sentiment()`:**
Im Return-Dict zusätzlich `_day_counts` liefern (Tages-Counts aus Artikel-Timestamps).
Direkt vor dem `return { ... }` einfügen:

```python
from collections import Counter  # oben bei den Imports einordnen
day_counts = Counter(
    date.fromtimestamp(a["datetime"]).isoformat()
    for a in news if a.get("datetime")
)
```

und im Return-Dict: `"_day_counts": dict(day_counts),`.
Auch im Zero-News-Fall (Zeile ~149) ergänzen: `"_day_counts": {}`.

**Änderung 2 — `scanner.py`, Funktion `run_scan()`:**
In der Stufe-1-Schleife (dort wo `_fetch_sentiment` erfolgreich war) die Tageszeilen
sammeln und gebündelt schreiben:

```python
# beim Schleifenstart:
buzz_rows = []

# nach erfolgreichem sentiment-Fetch für einen Ticker:
for day, cnt in sent.get("_day_counts", {}).items():
    buzz_rows.append((ticker, day, cnt, sent.get("bullish_pct", 0.0)))
if len(buzz_rows) >= 500:
    from signals_db import upsert_buzz_rows
    upsert_buzz_rows(buzz_rows)
    buzz_rows = []

# nach der Schleife (auch bei Abort):
if buzz_rows:
    from signals_db import upsert_buzz_rows
    upsert_buzz_rows(buzz_rows)
```

**Änderung 3 — `scanner.py`, vor `_write_results()`:**
An der Stelle, wo `_news_texts` entfernt wird (Zeile ~383–387), zusätzlich
`.pop("_day_counts", None)` auf denselben Dicts aufrufen. **Wichtig:** `_day_counts`
darf NIE in `results.json` landen.

**Hinweis:** `INSERT OR REPLACE` macht die zwei Scans/Tag idempotent — der 19:30-Scan
überschreibt die Werte des 13:00-Scans mit aktuelleren Counts. Kein Sonderfall nötig.

### 1.4 `.gitignore` ergänzen

```
signals.db
signals.db-wal
signals.db-shm
```

### 1.5 Scheduler-Registrierung in `app.py`

In `_reschedule()` am Ende ergänzen:

```python
# Frühsignale (EARLY_SIGNALS_UMSETZUNG.md)
if cfg.get("early_signals", {}).get("enabled", False):
    scheduler.add_job(
        _do_edgar_scan, "cron",
        hour="6-22", minute="*/15", day_of_week="mon-fri",
        timezone="America/New_York", id="edgar_scan",
    )
```

Und die Wrapper-Funktion (gleiches Muster wie `_do_full_scan`):

```python
def _do_edgar_scan():
    cfg = _load_cfg()
    if not cfg.get("early_signals", {}).get("enabled", False):
        return
    try:
        from layer1_edgar import run_edgar_scan
        run_edgar_scan(cfg)
    except Exception:
        log.exception("EDGAR-Scan fehlgeschlagen")
```

**`config.default.json`** um diesen Block ergänzen (config.json ist gitignored — auf dem
Server muss der Block nach dem Deploy manuell in die dortige config.json, oder Datei
löschen → `_load_cfg()` kopiert die Default neu; ACHTUNG: dann gehen dortige Einstellungen
verloren → Block manuell einfügen ist der richtige Weg):

```json
"early_signals": {
    "enabled": false,
    "insider_min_usd": 25000,
    "volume_z_min": 2.5,
    "buzz_rel_accel_min": 1.0,
    "alert_min_score": 4,
    "alert_min_types": 2,
    "alert_cooldown_days": 7
}
```

### 1.6 Phase-A-Tests (Definition of Done)

Lokal (macOS, ohne Server):
1. `python3 -c "from signals_db import init_db; init_db()"` → `signals.db` entsteht, kein Fehler.
2. Form-4-Parser gegen 2–3 echte Filings testen: auf
   https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=4 ein beliebiges
   Filing öffnen, dessen Index-URL an `_fetch_form4_xml()` + `_parse_form4()` geben,
   Ergebnis manuell mit dem Filing vergleichen (Symbol, Shares, Preis, Code P vs. S/M/A).
3. `run_edgar_scan(cfg)` einmal manuell mit `enabled=true`-Test-cfg laufen lassen →
   Log prüfen, `sqlite3 signals.db "SELECT * FROM signals"` prüfen.

Auf dem Server (nach Deploy):
4. `early_signals.enabled` in `/opt/sentiment-scanner/config.json` auf `true`,
   `systemctl restart sentiment-scanner`, dann `journalctl -u sentiment-scanner -f`
   → „EDGAR-Lauf fertig"-Zeilen alle 15 Min.
5. Nach dem nächsten Vollscan: `sqlite3 signals.db "SELECT COUNT(*) FROM buzz_history"`
   → mehrere tausend Zeilen.
6. `results.json` prüfen: darf KEIN `_day_counts` enthalten (`grep _day_counts results.json` → leer).

---

## 2. Phase B — Layer 2: Volumen-Anomalie (yfinance)

### 2.1 Dependency

`requirements.txt`: Zeile `yfinance` ergänzen. Auf dem Server:
`/opt/sentiment-scanner/venv/bin/pip install yfinance`.

### 2.2 Neue Datei `layer2_volume.py`

```python
"""Layer 2: Volumen-Anomalie (z-Score) ohne News-Begleitung. Quelle: yfinance EOD."""
import logging
import statistics
from datetime import datetime, timezone, date, timedelta

import yfinance as yf

from signals_db import get_conn, insert_signal

log = logging.getLogger("sentiment-scanner")
CHUNK = 200            # Ticker pro yf.download-Call
MIN_HISTORY = 21       # 20 Tage Basis + heutiger Tag


def _news_flat(ticker: str) -> bool:
    """True wenn News-Count der letzten 3 Tage <= Median der Tages-Counts (30 Tage)."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT date, news_count FROM buzz_history WHERE ticker=? "
            "AND date >= date('now', '-30 days')", (ticker,)).fetchall()
    if not rows:
        return True  # keine News-Historie = keine News = flach
    counts = {r["date"]: r["news_count"] for r in rows}
    cutoff = (date.today() - timedelta(days=3)).isoformat()
    recent = sum(c for d, c in counts.items() if d >= cutoff)
    med = statistics.median(counts.values())
    return recent <= max(med * 3, 3)  # 3 Tage vs. Tagesmedian → Faktor 3


def run_volume_scan(cfg: dict) -> None:
    from scanner import _load_tickers
    z_min = cfg.get("early_signals", {}).get("volume_z_min", 2.5)
    tickers = [t["symbol"] for t in _load_tickers()]
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    hits = 0

    for i in range(0, len(tickers), CHUNK):
        chunk = tickers[i:i + CHUNK]
        try:
            data = yf.download(tickers=chunk, period="2mo", interval="1d",
                               group_by="ticker", threads=True, progress=False,
                               auto_adjust=False)
        except Exception as e:
            log.warning("yfinance Chunk %d: %s", i, e)
            continue
        for sym in chunk:
            try:
                vol = data[sym]["Volume"].dropna() if len(chunk) > 1 else data["Volume"].dropna()
            except (KeyError, TypeError):
                continue
            if len(vol) < MIN_HISTORY:
                continue
            today_vol = float(vol.iloc[-1])
            base = [float(v) for v in vol.iloc[-21:-1]]
            mean_v = statistics.mean(base)
            sd_v = statistics.stdev(base)
            if sd_v == 0:
                continue
            z = (today_vol - mean_v) / sd_v
            if z < z_min or not _news_flat(sym):
                continue
            score = 3.0 if z >= 4.0 else 2.0
            insert_signal(sym, "volume_anomaly", now_iso, round(z, 2),
                          {"z_score": round(z, 2), "volume": today_vol,
                           "mean_20d": round(mean_v), "weight": score})
            hits += 1
    log.info("Volumen-Scan fertig: %d Anomalien", hits)
```

**Hinweise:**
- `yf.download` mit Liste liefert MultiIndex-Spalten (`data[sym]["Volume"]`); bei nur
  1 Ticker im Chunk flache Spalten — der try/except oben fängt beide Fälle.
  Beim Implementieren mit 2–3 Tickern lokal verifizieren, welche Struktur zurückkommt.
- Laufzeit für ~4700 Ticker in 200er-Chunks: einige Minuten. Kein Finnhub-Budget betroffen.
- Earnings-Kalender-Abgleich (Spec Abschnitt 3.3) bewusst NICHT in v1 — Free-Tier-Status
  von `/calendar/earnings` unklar. In `details_json` steht das Volumen; falscher Alarm an
  Earnings-Tagen wird über den Forward-Return-Tracker sichtbar und dann entschieden.

### 2.3 Layer 3 Teil 2 — Buzz-Beschleunigung, neue Datei `layer3_buzz.py`

```python
"""Layer 3: Buzz-Beschleunigung aus buzz_history (keine API-Calls)."""
import logging
from datetime import datetime, timezone, date, timedelta

from signals_db import get_conn, insert_signal

log = logging.getLogger("sentiment-scanner")


def run_buzz_accel(cfg: dict) -> None:
    es = cfg.get("early_signals", {})
    rel_min = es.get("buzz_rel_accel_min", 1.0)
    news_min = cfg.get("filter", {}).get("news_min_count", 3)
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    d = date.today()
    win_recent = [(d - timedelta(days=k)).isoformat() for k in range(0, 3)]   # d0..d2
    win_prev = [(d - timedelta(days=k)).isoformat() for k in range(3, 6)]     # d3..d5
    hits = 0

    with get_conn() as conn:
        tickers = [r["ticker"] for r in conn.execute(
            "SELECT DISTINCT ticker FROM buzz_history WHERE date >= date('now','-6 days')")]
        for t in tickers:
            rows = dict(conn.execute(
                "SELECT date, news_count FROM buzz_history WHERE ticker=? "
                "AND date >= date('now','-6 days')", (t,)).fetchall())
            recent = sum(rows.get(day, 0) for day in win_recent)
            prev = sum(rows.get(day, 0) for day in win_prev)
            rel = (recent - prev) / max(1, prev)
            weekly = recent + prev
            # feuert nur UNTER dem bestehenden Kandidaten-Level und mit Mindestsubstanz
            if rel >= rel_min and recent >= 3 and weekly <= news_min * 3:
                insert_signal(t, "buzz_accel", now_iso, round(rel, 2),
                              {"recent_3d": recent, "prev_3d": prev, "rel_accel": round(rel, 2)})
                hits += 1
    log.info("Buzz-Accel fertig: %d Signale", hits)
```

### 2.4 Scheduler-Jobs (app.py, `_reschedule()`, im `early_signals`-Block ergänzen)

```python
    scheduler.add_job(_do_volume_scan, "cron", hour=17, minute=15,
                      day_of_week="mon-fri", timezone="America/New_York", id="volume_scan")
    scheduler.add_job(_do_buzz_accel, "cron", hour=17, minute=25,
                      day_of_week="mon-fri", timezone="America/New_York", id="buzz_accel")
```

Wrapper analog `_do_edgar_scan` (Flag prüfen, Import im Funktionskörper, `log.exception`).
17:15 ET = nach Börsenschluss (16:00 ET) — DST-sicher, weil ET-basiert.

### 2.5 Phase-B-Tests

1. Lokal: `run_volume_scan(cfg)` mit einer auf ~20 Ticker gekürzten Ticker-Liste →
   z-Scores einer Aktie stichprobenartig gegen händische Rechnung (20-Tage-Mittel/SD) prüfen.
2. `sd == 0`-Fall mit illiquidem Ticker provozieren oder Unit-Test → kein Crash, kein Signal.
3. Server: nach einem Handelstag `sqlite3 signals.db "SELECT * FROM signals WHERE signal_type='volume_anomaly' ORDER BY id DESC LIMIT 10"`.

---

## 3. Phase C — Scoring, Alerts, Forward-Returns, PWA-Tab

### 3.1 Neue Datei `layer4_scoring.py`

```python
"""Layer 4: Kombinations-Scoring + Telegram-Alert. Läuft 1x täglich nach den EOD-Layern."""
import json
import logging
from datetime import datetime, timezone

from signals_db import get_conn

log = logging.getLogger("sentiment-scanner")


def run_scoring(cfg: dict) -> None:
    from scanner import _tg_post, _fetch_quote
    es = cfg.get("early_signals", {})
    min_score = es.get("alert_min_score", 4)
    min_types = es.get("alert_min_types", 2)
    cooldown = es.get("alert_cooldown_days", 7)
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")

    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, ticker, signal_type, signal_ts, score, details_json FROM signals "
            "WHERE signal_ts >= datetime('now', '-7 days')").fetchall()
    by_ticker: dict[str, list] = {}
    for r in rows:
        by_ticker.setdefault(r["ticker"], []).append(r)

    for ticker, sigs in by_ticker.items():
        types = {s["signal_type"] for s in sigs}
        if len(types) < min_types:
            continue
        # Gewichte: insider 3 (+2 Cluster), volume 2 (z>=4: 3), buzz 1 — pro Typ nur das stärkste Signal
        best: dict[str, float] = {}
        for s in sigs:
            d = json.loads(s["details_json"] or "{}")
            if s["signal_type"] == "insider_buy":
                w = 3.0 + (2.0 if d.get("cluster") else 0.0)
            elif s["signal_type"] == "volume_anomaly":
                w = 3.0 if d.get("z_score", 0) >= 4.0 else 2.0
            else:
                w = 1.0
            best[s["signal_type"]] = max(best.get(s["signal_type"], 0), w)
        total = sum(best.values())
        if total < min_score:
            continue

        with get_conn() as conn:
            recent_alert = conn.execute(
                "SELECT 1 FROM alerts WHERE ticker=? AND alert_ts >= datetime('now', ?)",
                (ticker, f"-{cooldown} days")).fetchone()
        if recent_alert:
            continue

        price = _fetch_quote(ticker)
        sig_ids = [s["id"] for s in sigs]
        with get_conn() as conn:
            cur = conn.execute(
                "INSERT INTO alerts (ticker, alert_ts, total_score, signal_ids, price_at_alert) "
                "VALUES (?, ?, ?, ?, ?)",
                (ticker, now_iso, total, json.dumps(sig_ids), price))
            alert_id = cur.lastrowid
            conn.executemany(
                "INSERT OR IGNORE INTO forward_returns (alert_id, horizon_days) VALUES (?, ?)",
                [(alert_id, h) for h in (1, 5, 20)])

        lines = [f"🔮 <b>Frühsignal: {ticker}</b> — Score {total:.0f}"]
        for s in sorted(sigs, key=lambda x: x["signal_ts"]):
            d = json.loads(s["details_json"] or "{}")
            extra = d.get("filing_url") or f"z={d.get('z_score')}" if s["signal_type"] != "buzz_accel" else f"accel={d.get('rel_accel')}"
            lines.append(f"• {s['signal_type']} {s['signal_ts'][:16]} {extra}")
        lines.append(f"Kurs: {price if price else '–'} USD | kein Anlagerat, Validierung läuft")
        _tg_post("\n".join(lines))
        log.info("Frühsignal-Alert: %s score=%.0f", ticker, total)
```

**Vorher prüfen:** Signatur/HTML-Format von `_tg_post` in `scanner.py` ansehen und die
Nachricht dem bestehenden Format angleichen (parse_mode etc.).

### 3.2 Neue Datei `forward_tracker.py`

```python
"""Füllt forward_returns wenn der Handelstage-Horizont (1/5/20) erreicht ist."""
import logging
from datetime import datetime, timezone

import yfinance as yf

from signals_db import get_conn

log = logging.getLogger("sentiment-scanner")


def run_tracker(cfg: dict) -> None:
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with get_conn() as conn:
        open_rows = conn.execute(
            "SELECT fr.alert_id, fr.horizon_days, a.ticker, a.alert_ts, a.price_at_alert "
            "FROM forward_returns fr JOIN alerts a ON a.id = fr.alert_id "
            "WHERE fr.ret_pct IS NULL AND a.price_at_alert IS NOT NULL").fetchall()
    filled = 0
    for r in open_rows:
        try:
            hist = yf.download(r["ticker"], start=r["alert_ts"][:10], interval="1d",
                               progress=False, auto_adjust=False)
            closes = hist["Close"].dropna()
            # Zeile 0 = Alert-Tag; Horizont h = h Handelstage danach
            if len(closes) <= r["horizon_days"]:
                continue
            ret = (float(closes.iloc[r["horizon_days"]]) / r["price_at_alert"] - 1) * 100
            with get_conn() as conn:
                conn.execute(
                    "UPDATE forward_returns SET ret_pct=?, filled_ts=? "
                    "WHERE alert_id=? AND horizon_days=?",
                    (round(ret, 2), now_iso, r["alert_id"], r["horizon_days"]))
            filled += 1
        except Exception as e:
            log.warning("Tracker %s h=%d: %s", r["ticker"], r["horizon_days"], e)
    log.info("Forward-Tracker: %d Returns gefüllt", filled)
```

### 3.3 Scheduler (im `early_signals`-Block)

```python
    scheduler.add_job(_do_scoring, "cron", hour=17, minute=35,
                      day_of_week="mon-fri", timezone="America/New_York", id="es_scoring")
    scheduler.add_job(_do_fwd_tracker, "cron", hour=17, minute=45,
                      day_of_week="mon-fri", timezone="America/New_York", id="es_tracker")
```

### 3.4 API-Endpoint in `app.py`

```python
@app.route("/api/early-signals")
def api_early_signals():
    from signals_db import get_conn
    with get_conn() as conn:
        signals = [dict(r) for r in conn.execute(
            "SELECT ticker, signal_type, signal_ts, score, details_json FROM signals "
            "ORDER BY signal_ts DESC LIMIT 100")]
        alerts = [dict(r) for r in conn.execute(
            "SELECT * FROM alerts ORDER BY alert_ts DESC LIMIT 50")]
        stats = [dict(r) for r in conn.execute(
            "SELECT horizon_days, COUNT(*) n, AVG(ret_pct) avg_ret, "
            "SUM(CASE WHEN ret_pct > 0 THEN 1 ELSE 0 END)*100.0/COUNT(*) hit_rate "
            "FROM forward_returns WHERE ret_pct IS NOT NULL GROUP BY horizon_days")]
    return jsonify({"signals": signals, "alerts": alerts, "stats": stats})
```

### 3.5 PWA-Tab „Frühsignale"

- **VORHER `BKM/PWA-Standards.md` vollständig lesen** und die dortige Checkliste einhalten.
- Fünfter Tab in `pwa/index.html`, gleiche Optik wie bestehende Tabs.
- Inhalt: (a) Alert-Liste (Ticker, Datum, Score, beteiligte Signale, Kurs bei Alert),
  (b) Signal-Feed (letzte 100), (c) Statistik-Box: Trefferquote + Ø-Return je Horizont
  aus `stats` — mit Hinweis „Validierungsphase, aussagekräftig erst nach 2–3 Monaten".
- Info-Bereich der App aktualisieren (neuer Tab erklären) + **Versionsnummer Y+1 und
  Datum** gemäß `BKM/App-Versionierung.md`.
- `sw.js`: Cache-Name hochzählen (`sentiment-v1` → nächste Nummer), sonst sehen Clients
  den neuen Tab nicht.

### 3.6 Phase-C-Tests

1. Test-Alert erzwingen: zwei künstliche Signale verschiedenen Typs für einen Ticker in
   `signals` inserten → `run_scoring(cfg)` → Telegram-Nachricht kommt an, `alerts`-Zeile
   + 3 offene `forward_returns`-Zeilen existieren. Testdaten danach löschen.
2. Cooldown: `run_scoring` direkt nochmal → KEIN zweiter Alert.
3. Tracker: künstlichen Alert mit `alert_ts` vor 10 Tagen anlegen → `run_tracker` füllt
   Horizonte 1 und 5, lässt 20 offen.
4. PWA-Tab auf iPhone prüfen (Layout, Dark Mode falls vorhanden).

### 3.7 ADR schreiben (nach Phase C)

`ADR/ADR-006-fruehsignal-layer.md` nach dem Template `~/Dropbox/Apps/Claude/PKA/BKM/ADR-Template.md`.
Inhalt: Entscheidung für EDGAR+yfinance+SQLite/WAL+APScheduler; verworfene Alternativen:
Finnhub-Candles (403 Free Tier), Cron statt APScheduler (Architekturbruch),
Social-Media-Layer (API-Bedingungen unklar, bewusst nicht in v1).

---

## 4. Deployment (je Phase identisch)

```bash
# Lokal
git -C ~/Library/CloudStorage/Dropbox/Apps/Claude/Stock\ Sentiment\ Scanner add -A
git -C ~/Library/CloudStorage/Dropbox/Apps/Claude/Stock\ Sentiment\ Scanner commit -m "Frühsignale Phase X"
git -C ~/Library/CloudStorage/Dropbox/Apps/Claude/Stock\ Sentiment\ Scanner push

# Server
ssh root@89.167.104.145 "cd /opt/sentiment-scanner && git pull && venv/bin/pip install -r requirements.txt -q && chown -R webhook:webhook . && systemctl restart sentiment-scanner && journalctl -u sentiment-scanner -n 20 --no-pager"
```

Nach Phase A zusätzlich einmalig: `early_signals`-Block manuell in die Server-`config.json`
einfügen (siehe 1.5), `enabled` erst nach erfolgreichem Test 1.6/2 auf `true`.
`signals.db` muss `webhook:webhook` gehören (der `chown` oben erledigt das).

## 5. Erfolgskontrolle gesamt

- Nach 1 Woche: `signals`-Tabelle enthält alle drei Typen; keine ERROR-Zeilen im journalctl.
- Nach 2–3 Monaten: Statistik-Box im PWA-Tab auswerten (Trefferquote je Horizont vs.
  Zufall/IWM). **Erst dann** Schwellwerte in config anpassen — nie nach Einzelfällen.
