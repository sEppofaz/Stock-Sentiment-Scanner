# ADR-010: Gemeinsamer yf_helper.py statt Duplikat oder Mega-Datei

**Datum:** 2026-08-06
**Status:** aktiv
**Projekt:** Stock Sentiment Scanner

## Problem

Der neue `scan_tracker.py` (füllt `scan_forward_returns` für die Sentiment-Scan-Snapshot-Historie) braucht denselben yfinance-Zugriff wie der bestehende `forward_tracker.py` (füllt `forward_returns` für Frühsignal-Alerts) – inklusive des bereits dokumentierten Pitfalls, dass `yf.download()` auch bei einem einzelnen String-Ticker MultiIndex-Spalten liefert (`hist["Close"][ticker]` statt `hist["Close"]` direkt, sonst crasht `float(...)`, verifiziert 2026-07-06). Wie den Code teilen, ohne die beiden Tracker unnötig zu verkoppeln?

## Entscheidung

Neue Datei `yf_helper.py` mit einer einzigen Funktion `fetch_closes(ticker, start_date)`, die den yfinance-Download + MultiIndex-Fix kapselt. `forward_tracker.py` und `scan_tracker.py` importieren beide `fetch_closes()`, bleiben ansonsten komplett unabhängig (eigene SQL-Queries, eigene Tabellen, eigene Baseline-Logik).

## Begründung

- Der tatsächlich fehleranfällige, wiederholungsträchtige Teil ist ausschließlich der yfinance-Aufruf samt Pitfall – **nur den** extrahieren, nicht die umgebende SQL-/Gruppierungslogik.
- Die SQL-Flows unterscheiden sich strukturell genug (unterschiedliche Tabellen `forward_returns` vs. `scan_forward_returns`, unterschiedliche Baseline-Ermittlung – `alerts.price_at_alert` kommt sofort von Finnhub, `scan_snapshots.price_at_snapshot` wird erst vom Tracker selbst aus `closes.iloc[0]` gesetzt, siehe ADR-009), dass eine gemeinsame Funktion mit Tabellennamen-Parametern mehr bedingte Verzweigungen gebraucht hätte als sie Zeilen gespart hätte.
- Ein zukünftiger Fix am yfinance-Zugriff (z.B. Retry-Logik, anderer Datenanbieter) muss dadurch nur an einer Stelle gepflegt werden.

## Verworfen

| Alternative | Warum verworfen |
|---|---|
| Komplette Code-Duplikation (yfinance-Block 1:1 in `scan_tracker.py` kopiert) | Der dokumentierte Pitfall müsste an zwei Stellen synchron gehalten werden – Risiko, dass ein künftiger Fix nur in einer Datei landet |
| Eine gemeinsame Funktion mit Tabellennamen-/Spalten-Parametern für den gesamten Tracker-Ablauf (nicht nur den yfinance-Teil) | Mehr Bedingungslogik (unterschiedliche JOINs, unterschiedliche Baseline-Behandlung) als Ersparnis; hätte die klare 1:1-Lesbarkeit beider Tracker-Dateien gegenüber ihren jeweiligen Vorlagen zerstört |

## Gilt unter

Gilt solange beide Tracker denselben Datenanbieter (yfinance, Tages-Close-Reihen) nutzen. Wechselt einer der beiden Tracker den Datenanbieter oder die Granularität (z.B. Intraday statt Tages-Close), muss der gemeinsame Helper neu bewertet werden.

## Konsequenzen

- `yf_helper.py` ist die einzige Stelle im Projekt, die `import yfinance` direkt enthält – `forward_tracker.py`/`scan_tracker.py` importieren nur noch `fetch_closes`.
- Verhalten von `forward_tracker.py` bleibt nach dem Refactoring unverändert (nur der Download-Block wurde durch den Helper-Aufruf ersetzt, keine Logikänderung).
