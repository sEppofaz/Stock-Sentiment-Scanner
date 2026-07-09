# ADR-007: Tages-Cap für Instant-Alerts (stärkste zuerst, dynamische Verdrängung)

**Datum:** 2026-07-09
**Status:** aktiv
**Projekt:** Stock Sentiment Scanner

## Problem
`check_instant_alerts()` prüft alle 15 Min neue Signale auf Einzelsignal-Stärke und löst pro qualifizierendem Signal sofort und unabhängig einen Telegram-Alert aus – ohne Obergrenze pro Tag. Josef-Feedback (2026-07-09): zu viele Telegram-Nachrichten, er will nur die wenigen wirklich besten Opportunities pro Tag ("nur die 1–3 besten", später konkretisiert auf "5 pro Tag und immer den besten der aktuellen session, wenn er besser oder stärker ist als die bisherigen").

## Entscheidung
Ein Tages-Cap (`early_signals.max_instant_alerts_per_day`, Default 5) begrenzt, wie viele Instant-Alerts als "Top N des Tages" gelten. Pro 15-Min-Lauf werden qualifizierende Kandidaten zuerst nach ihrem rohen Signalwert (`score`) absteigend sortiert. Für jeden Kandidaten wird geprüft: Ist noch ein Slot frei (weniger als N Instant-Alerts heute) ODER ist der Kandidat stärker als der aktuell schwächste unter den heutigen Top-N? Nur dann wird `_create_alert()` aufgerufen. Der Handelstag wird über den Kalendertag in `America/New_York` bestimmt (nicht UTC), passend zur "Session"-Formulierung von Josef und zum bestehenden 6–22-Uhr-ET-Fenster des Jobs. Eine neue Spalte `alerts.kind` (`instant`/`combo`) trennt diese Zählung vom täglichen Kombi-Alert aus `run_scoring()`, der von diesem Cap unberührt bleibt.

## Begründung
- Bildet Josefs Anforderung direkt ab: nicht "die ersten N", sondern "die besten N" – ein später am Tag eintreffendes stärkeres Signal verdrängt das bisher schwächste der Top-N.
- Nutzt die ohnehin vorhandene `alerts`-Tabelle als Datenquelle für "was wurde heute schon als Top-N geführt" – keine neue Infrastruktur nötig.
- Reine additive Änderung: bestehende Schwellenwert-Filter (`single_insider_min_usd` etc.) und der 7-Tage-Cooldown pro Ticker bleiben unverändert vorgeschaltet, der Cap greift erst danach.

## Verworfen

| Alternative | Warum verworfen |
|---|---|
| `total_score` über alle Signaltypen normieren (z. B. "Vielfaches der Schwelle" statt Rohwert), um einen fairen Cross-Type-Vergleich zu ermöglichen | Hätte historische `total_score`-Werte (Statistik/Trefferquote) rückwirkend inkonsistent zu neuen gemacht und ging über den expliziten Auftrag hinaus (Josef wollte für die Zahl im Alert nur "Erklärung ergänzen", keine Neuberechnung) |
| Harter Cap ohne Verdrängung ("die ersten 5 pro Tag, danach Funkstille") | Widerspricht Josefs expliziter Anforderung "immer den besten … wenn er besser oder stärker ist als die bisherigen" |
| Beim Verdrängen die schwächere, bereits gesendete Alert-Zeile aus `alerts` löschen | `alerts`/`forward_returns` sind laut bestehender `cleanup_old_data()`-Doku bewusst die dauerhafte Validierungshistorie (Trefferquote/Rendite) und werden nie automatisch gelöscht – Löschen hätte das gebrochen |

## Gilt unter
- Handelstag-Grenze = Kalendertag `America/New_York`, nicht UTC oder Berlin-Lokalzeit.
- Gilt ausschließlich für `kind='instant'`. Der tägliche Kombi-Alert (`kind='combo'`) hat kein eigenes Tages-Limit, ist aber durch seine eigenen Schwellen (≥2 Signal-Typen, Mindest-Score) schon selten.
- Setzt voraus, dass `score`-Werte innerhalb eines Laufs grob als Rangordnung taugen, auch wenn sie zwischen Signaltypen nicht auf derselben Skala liegen (dokumentierter Pitfall, siehe CLAUDE.md "Pitfall Score-Skala").

## Konsequenzen
- Bereits versendete Telegram-Nachrichten werden nicht zurückgeholt: An volatilen Tagen mit mehreren "stärker als bisher schwächster"-Ereignissen kann die tatsächliche Anzahl versendeter Nachrichten über 5 liegen. Das Limit begrenzt die Größe der geführten "Top-N des Tages", nicht hart die Zahl der Telegram-Pushes.
- Cross-Type-Vergleich bleibt unnormiert – ein Signaltyp mit strukturell höheren Rohwerten (z. B. Volumen-z-Score) kann die Top-N-Slots systematisch häufiger belegen als ein anderer. Nicht behoben, nur dokumentiert; bei Bedarf später revisitierbar, sobald genug Live-Daten für eine fundierte Normierung vorliegen.
