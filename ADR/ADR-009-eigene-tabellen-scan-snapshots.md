# ADR-009: Eigene Tabellen (scan_snapshots/scan_forward_returns) statt Erweiterung von alerts/forward_returns

**Datum:** 2026-08-06
**Status:** aktiv
**Projekt:** Stock Sentiment Scanner

## Problem

Für die neue wöchentliche Performance-Analyse (Josef-Wunsch: sehen was positiv vs. negativ performende Empfehlungen unterscheidet) fehlte für den Sentiment-Scan (Top-N-Ergebnisse aus `run_scan()`) jede Historie – `results.json` wird bei jedem Vollscan komplett überschrieben. Für Frühsignale existiert bereits `alerts`/`forward_returns` in `signals.db`. Frage: neue eigene Tabellen für den Sentiment-Scan, oder das bestehende `alerts`-Schema mitnutzen?

## Entscheidung

Neue eigene Tabellen `scan_snapshots` (Score-Komponenten je Top-N-Ticker pro Vollscan) und `scan_forward_returns` (Kursrendite 1/5/20 Handelstage später), statt `alerts`/`forward_returns` zu erweitern.

## Begründung

- Ein Scan-Snapshot ist kein Alert-Ereignis (kein ausgelöstes Signal, keine Schwellenwert-Überschreitung) – `alerts.signal_ids` würde für Snapshots strukturell leer/NULL bleiben, `alerts.kind` bräuchte einen dritten Wert nur für diesen Fall.
- Referenzkurs-Ermittlung unterscheidet sich bewusst (siehe „Gilt unter"): `alerts.price_at_alert` kommt sofort per Finnhub-Quote, `scan_snapshots.price_at_snapshot` bleibt absichtlich NULL bis der Tracker per yfinance nachträgt (0 zusätzliche Finnhub-Calls). Eine gemeinsame Tabelle hätte diese unterschiedliche Befüll-Semantik verschleiert.
- Jeder Vollscan erzeugt bis zu `top_n_results` (Default 50) Snapshot-Zeilen, 2×/Tag – deutlich höheres Volumen als Alerts (seltene Einzelereignisse). Getrennte Tabellen vermeiden, dass ein `source`-Diskriminator-Feld mit vielen strukturell ungenutzten Spalten in einer gemeinsamen Tabelle nötig wird (Schema-Smell).

## Verworfen

| Alternative | Warum verworfen |
|---|---|
| `alerts`/`forward_returns` um ein `source`-Feld ('scan'\|'signal') erweitern, Snapshot als Spezialfall eines Alerts modellieren | Viele Spalten (`signal_ids`, `kind`) wären für Scan-Snapshots bedeutungslos; hätte die für Frühsignale bereits produktiv laufende Tabelle riskant angefasst statt additiv zu erweitern |
| Snapshot-Historie in `results.json`-Rotationsdateien (z.B. `results-YYYYMMDD.json`) statt SQLite | Kein einfaches Gruppieren/Aggregieren über Wochen hinweg ohne eigenes Parsing; SQLite-Infrastruktur (`get_conn()`, WAL) existiert bereits und wird für Frühsignale produktiv genutzt |

## Gilt unter

Gilt solange der Sentiment-Scan (Top-N aus `run_scan()`) und die Frühsignale (`alerts`) strukturell getrennte Feature-Sets und Auslösemechanismen haben. Würden beide Systeme fusioniert (z.B. ein einheitlicher Empfehlungs-Score über alle Quellen), wäre eine Neubewertung sinnvoll.

## Konsequenzen

- Zwei parallele, strukturell ähnliche aber unabhängige Tracking-Pfade (`forward_tracker.py`/`scan_tracker.py`) – Code-Duplikation beim yfinance-Zugriff wurde über einen gemeinsamen Helper vermieden (siehe ADR-010).
- `weekly_analysis.py` muss beide Datenquellen separat abfragen und darf sie nicht vermischen – das entspricht aber ohnehin Josefs expliziter Anforderung nach getrennter Analyse je System.
- `cleanup_old_data()` musste um die drei neuen Tabellen NICHT erweitert werden (sie bleiben wie `alerts`/`forward_returns` unbegrenzt als Validierungshistorie erhalten) – nur der Docstring wurde aktualisiert.
