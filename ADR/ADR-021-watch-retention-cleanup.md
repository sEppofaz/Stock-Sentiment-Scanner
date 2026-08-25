# ADR-021: Automatisches Cleanup alter Auto-Watch-Beobachtungen (30 Tage)

**Datum:** 2026-08-25
**Status:** aktiv
**Projekt:** Stock Sentiment Scanner

## Problem

Bei der Live-Verifikation von ADR-020 gefunden: `portfolio.json` ist auf 746 Einträge gewachsen (743 automatisch angelegte Frühsignal-Beobachtungen aus `_auto_watch()`, seit Einführung 2026-07-08). Der 15-Minuten-Portfolio-Scan-Cronjob (`_do_portfolio_scan`) fragt für jeden nicht-geschlossenen Eintrag Sentiment + Kurs ab (bis zu 2 Finnhub-Calls) – bei 744 offenen Einträgen und dem globalen 55-Calls/Minute-Throttle braucht ein vollständiger Durchlauf rechnerisch ~27+ Minuten, also länger als das 15-Minuten-Intervall selbst. Josef bat direkt darum, die Einträge „auf ein Maß zu reduzieren, das für künftige Auswertungen und Analysen notwendig ist".

## Entscheidung

Neue Funktion `scanner.cleanup_stale_watches()`, täglich im bestehenden `_do_cleanup()`-Job (03:00 UTC) ausgeführt: entfernt alle `portfolio.json`-Einträge mit `watch=True` und `closed=False`, deren `buy_date` älter als 30 Tage ist. Echte Positionen (`watch=False`) und geschlossene Positionen (`closed=True`) sind davon nie betroffen.

## Begründung

- **Geprüft, nicht angenommen:** `weekly_analysis.py` (Performance-Analyse, Median/Trefferquote) und `forward_tracker.py` (füllt `forward_returns`) greifen beide ausschließlich auf `signals.db` zu (`alerts`/`forward_returns`/`scan_snapshots`/`scan_forward_returns`) – keiner der beiden liest `portfolio.json` oder dessen `watch`-Einträge. Per Grep verifiziert (kein Treffer für `_load_portfolio`/`portfolio.json`/`watch` in `weekly_analysis.py`). Ein entfernter Watch-Eintrag verliert also **keine** Auswertungsfähigkeit.
- **30-Tage-Schwelle:** knapp über dem längsten Auswertungs-Horizont von `forward_returns` (20 Handelstage ≈ 28 Kalendertage) – die App-interne Anzeige „Rendite seit Signal" bleibt für den vollen relevanten Zeitraum sichtbar, bevor der Eintrag verschwindet. Von Josef nach Abwägung dreier Optionen (14/30/60 Tage) explizit gewählt.
- **Wiederverwendung des bestehenden Cleanup-Jobs** (`_do_cleanup`, bereits täglich für `buzz_history`/`edgar_seen`) statt eines neuen Scheduler-Eintrags – gleiche Kadenz, gleiches Muster (`cleanup_old_data()` als Vorbild), eigener try/except-Block, damit ein Fehler im einen Cleanup-Pfad den anderen nicht blockiert.
- **`_update_portfolio()`-Lock verwendet** (Atomic-Write-Pattern), damit das Cleanup nicht mit einem gleichzeitig laufenden Portfolio-Scan/Web-Endpoint kollidiert.

## Verworfen

| Alternative | Warum verworfen |
|---|---|
| Beobachtungen sofort nach Alert-Erstellung nicht mehr live scannen (nur einmalig anlegen) | Hätte die App-interne "Rendite seit Signal"-Anzeige für aktuelle Alerts (der eigentliche Zweck von Auto-Watch) direkt nach Anlage eingefroren – kein sinnvoller Kompromiss |
| Auto-Watch-Feature komplett abschalten (`early_signals.auto_watch: false`) | Hätte die Live-Anzeige für ALLE (auch neue) Alerts abgeschafft, nicht nur die alten – Josef wollte reduzieren, nicht das Feature verlieren |
| Konfigurierbarer Retention-Wert in `config.json` | Bestehende Cleanup-Schwellen (`buzz_history` 60 Tage, `edgar_seen` 30 Tage) sind ebenfalls hardcoded, nicht konfigurierbar – Konsistenz mit etabliertem Muster, kein zusätzlicher Config-Schlüssel für einen Wert, der sich kaum ändern dürfte |

## Gilt unter

Setzt voraus, dass `weekly_analysis.py`/`forward_tracker.py` weiterhin ausschließlich `signals.db` nutzen. Sollte künftig eine Funktion entstehen, die tatsächlich auf `watch=True`-Einträge in `portfolio.json` für Auswertungszwecke zugreift, muss diese ADR überprüft werden.

## Konsequenzen

- `portfolio.json` bleibt dauerhaft klein (nur Beobachtungen der letzten 30 Tage + echte/geschlossene Positionen) – Portfolio-Scan-Dauer bleibt im Rahmen des 15-Minuten-Intervalls.
- Ältere Frühsignal-Alerts verschwinden nach 30 Tagen aus der Live-Kartenansicht im Früh-/Portfolio-Tab; ihre Performance bleibt aber vollständig in `signals.db` nachvollziehbar (Analyse-Tab, wöchentliche Performance-Analyse).
- Info-Bereich der App entsprechend ergänzt (v1.29).
