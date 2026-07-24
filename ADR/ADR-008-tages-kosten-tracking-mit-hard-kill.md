# ADR-008: Tages-Kosten-Tracking mit Hard-Kill bei 5$ (zusätzlich zur Lifetime-EUR-Schwelle)

**Datum:** 2026-07-24
**Status:** aktiv
**Projekt:** Stock Sentiment Scanner

## Problem

`_update_claude_costs()` trackt bereits Kosten, aber nur als Lifetime-Summe in EUR mit einer Einmal-pro-ganzzahlige-Schwelle-Benachrichtigung (`last_threshold_notified`). Es gibt keine Tages-Grenze und keinen automatischen Abbruch, falls an einem Tag unerwartet viele/teure Claude-Calls anfallen (z.B. durch einen Bug, der mehrfach scannt). Der bestehende `SCAN_STATUS["abort"]`-Mechanismus wird zudem innerhalb von `_claude_enrich_batch()` gar nicht geprüft – ein während der Claude-Phase gedrückter Abbruch griff bisher erst nach Abschluss aller Batches.

## Entscheidung

Zusätzlich zur bestehenden Lifetime-EUR-Logik (bleibt unverändert) wird über `costs.py` ein paralleles Tages-Tracking in USD eingeführt (Session = Kalendertag, wie im Newsletter-Digest-Projekt, siehe dortige ADR-004 und `PKA/BKM/Claude-API-Kosten-Tracking.md`):
- **1$/Tag:** Telegram-Info, Scan läuft normal weiter.
- **5$/Tag:** `SCAN_STATUS["abort"] = True` wird gesetzt und die Batch-Schleife in `_claude_enrich_batch()` bricht sofort ab (nicht erst beim nächsten äußeren Check) – bereits angereicherte Ticker behalten ihr Ergebnis, der bestehende Abort-Skip-Pfad (Stufe 2 wird übersprungen) greift danach wie gehabt.

## Begründung

- Wiederverwendung des bestehenden `SCAN_STATUS["abort"]`-Flags statt eines zweiten Abbruch-Mechanismus – konsistent mit der bestehenden Architektur.
- USD statt EUR, konsistent mit der projektübergreifenden Entscheidung (Newsletter Digest, BKM-Standard) – Anthropic rechnet nativ in USD ab.
- Additiv statt ersetzend: Die bestehende Lifetime-EUR-Schwelle bleibt ein eigenständiges, weiterhin gültiges Signal ("Gesamtausgaben seit Projektstart haben wieder einen vollen Euro überschritten") und wird nicht angetastet.

## Verworfen

| Alternative | Warum verworfen |
|---|---|
| Bestehende `last_threshold_notified`/`total_cost_eur`-Logik auf USD umstellen bzw. ersetzen | Hätte den bereits produktiven Lifetime-Mechanismus gebrochen bzw. einen rückwirkenden Wechselkurs erfordert – sauberer als zwei unabhängige, nebeneinander laufende Zähler |
| Abbruch erst am nächsten äußeren `SCAN_STATUS["abort"]`-Check (nach `_claude_enrich_batch()`) | Ein weiterer voller Batch (bis zu 10 Ticker) würde noch unnötig abgerechnet, bevor der Abbruch greift – Check muss innerhalb der Batch-Schleife selbst erfolgen |
| Gemeinsames Kosten-Modul für alle Claude-Projekte (Package-Import) | Jedes Projekt ist ein eigenständiges Repo/venv – kein Cross-Repo-Import möglich, daher Copy-Paste-Template (siehe BKM) |

## Gilt unter

- `_claude_enrich_batch()` bleibt der einzige Claude-Aufrufpfad im Projekt (Portfolio-Scan ruft laut bestehender Architektur kein Claude auf).
- Server-Systemzeit (Europe/Berlin) bestimmt die Tagesgrenze für das neue USD-Tracking – unabhängig von der `America/New_York`-Zeitbasis der Frühsignal-Jobs (ADR-004/ADR-007), da es sich um verschiedene Zeitbezüge handelt (Kosten = Josefs wahrgenommener Tag, Handelssignale = US-Börsenzeit).

## Konsequenzen

+ Ein Kostenausreißer während der Claude-Anreicherung wird jetzt spätestens nach dem Batch erkannt, der die 5$-Schwelle überschreitet, nicht erst am Scan-Ende.
+ Lifetime-EUR-Schwelle bleibt für Josef als vertraute Langzeit-Kennzahl erhalten.
- Zwei parallele Kosten-Zähler (EUR-Lifetime, USD-Tages/Woche/Monat/Jahr) in derselben Datei – bewusst in Kauf genommen, um keinen impliziten Wechselkurs einzuführen.
