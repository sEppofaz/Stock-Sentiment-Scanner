# ADR-011: Referenz statt Duplikat für Pick-Forward-Returns

**Datum:** 2026-08-09
**Status:** aktiv
**Projekt:** Stock Sentiment Scanner

## Problem

Layer 6 (Tages-Konsolidierung) braucht eine neue `daily_picks`-Tabelle, um pro Handelstag höchstens einen Kauf-Pick zu persistieren. Um die spätere Performance des Picks zu verfolgen, wird eine Renditeverfolgung (1/5/20 Handelstage) benötigt – genau die Funktion, die für `alerts` (`forward_returns`) und `scan_snapshots` (`scan_forward_returns`) bereits existiert.

## Entscheidung

`daily_picks` bekommt KEINE eigene Forward-Returns-Tabelle. Stattdessen referenziert sie per Fremdschlüssel entweder `source_alert_id` (auf `alerts.id`) oder `source_snapshot_id` (auf `scan_snapshots.id`) – je nachdem, aus welcher Quelle der Pick stammte. Die Rendite-Historie des referenzierten Datensatzes (bereits von `forward_tracker.py`/`scan_tracker.py` gepflegt) dient als Renditeverfolgung des Picks.

## Begründung

- Ein Tages-Pick ist immer bereits ein Alert oder ein Scan-Snapshot, dessen Rendite ohnehin schon getrackt wird – eine zweite unabhängige Kursverfolgung für denselben Ticker/Zeitpunkt wäre reine Duplikation (zusätzliche yfinance-Calls, zusätzliche Tracker-Logik, zwei Quellen der Wahrheit für dieselbe Zahl).
- Für den Cross-Signal-Fall (`source='cross_signal'`) existieren sogar potenziell beide Referenzen gleichzeitig – auch das spricht für Referenz statt Duplikat, sonst müsste bei Divergenz entschieden werden, welche der beiden Kopien maßgeblich ist.
- Gleiches Muster wie bereits bei ADR-009 (`scan_snapshots` referenziert keinen zusätzlichen Preis-Fetch, sondern nutzt den ohnehin vom Tracker geholten Kurs).

## Verworfen

| Alternative | Warum verworfen |
|---|---|
| Eigene `daily_pick_forward_returns`-Tabelle, analog `forward_returns`/`scan_forward_returns` | Reine Duplikation derselben Daten, zusätzliche Tracker-Läufe/API-Calls ohne neuen Erkenntniswert |
| `daily_picks` speichert die Rendite direkt als Spalte (denormalisiert) | Müsste bei jedem Tracker-Lauf synchron nachgezogen werden – zusätzliche Kopplung ohne Vorteil ggü. einem simplen JOIN über die Fremdschlüssel |

## Gilt unter

Setzt voraus, dass jeder Pick tatsächlich aus einem bestehenden `alerts`- oder `scan_snapshots`-Eintrag hervorgeht (nie ein eigenständiger, nicht anderweitig getrackter Kandidat) – das ist strukturell garantiert, da `layer6_daily_pick.py` seine Kandidaten ausschließlich aus diesen beiden Tabellen sammelt.

## Konsequenzen

- Auswertung der Pick-Performance braucht einen JOIN über `source_alert_id`/`source_snapshot_id` in die jeweilige Forward-Returns-Tabelle statt eines direkten Spalten-Reads.
- `daily_picks` bleibt schlank und bekommt keine eigene Tracker-Pflicht im Scheduler.
