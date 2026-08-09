# ADR-012: Verkaufssignal nutzt bestehende sell_signal/sell_reason-Felder statt eigener Tabelle/UI

**Datum:** 2026-08-09
**Status:** aktiv
**Projekt:** Stock Sentiment Scanner

## Problem

Josef wollte ein zusätzliches Verkaufssignal auf Basis der Frühsignal-Gegensignale (Insider-Verkauf, Volumen-Anomalie bei fallendem Kurs), zeitnah geprüft (alle paar Minuten während der Börsenzeit), als Telegram- + App-Alert – aber nur für echte, bestätigte Positionen, nicht für automatisch angelegte Beobachtungen. `portfolio.json`-Einträge hatten dafür bereits `sell_signal`/`sell_reason`-Felder aus der bestehenden Sentiment-Umschwung-Logik (`scanner._check_sell_signal()`), inklusive vollständiger PWA-Unterstützung (🔴-Banner, `resetSignal()`-Button).

## Entscheidung

`layer6_sell_signal.py` setzt aus einer zweiten, unabhängigen Quelle (Frühsignal-Gegensignale) **dieselben** `sell_signal`/`sell_reason`-Felder wie die bestehende Sentiment-Logik – keine eigene Tabelle, kein neuer PWA-Code für die Anzeige. Zusätzlich neues internes Feld `sell_signal_source` (`"sentiment"` | `"fruehsignal"`), das NICHT in der UI angezeigt wird, sondern nur intern steuert, welcher Reset-Pfad ein aktives Signal wieder löschen darf.

## Begründung

- Maximale Wiederverwendung: die komplette Anzeige-Infrastruktur (Banner, Reset-Button, PWA-Styling) existierte bereits vollständig und funktioniert ohne jede Änderung mit der neuen Quelle.
- Ohne `sell_signal_source` hätte sich ein realer Bug ergeben (Cross-Contamination): `_run_portfolio_scan_inner()` setzte `sell_signal=False` bisher automatisch zurück, sobald die Stimmung wieder gut aussah – unabhängig von der ursprünglichen Ursache. Ein Frühsignal-Sell-Flag (z.B. Insider-Verkauf) wäre dadurch stillschweigend gelöscht worden, obwohl der eigentliche Auslöser (der Insider-Verkauf selbst) nie aufgelöst wurde. Das Feld ist der minimale Eingriff, um diesen Bug zu verhindern, ohne die bestehende Sentiment-Reset-Logik für den Normalfall zu verändern.
- Scope-Beschränkung auf echte Positionen (`watch=false`) direkt in `layer6_sell_signal.py` umgesetzt (Filter vor der Signal-Prüfung), nicht als generisches Flag – entspricht exakt Josefs Klarstellung ("Verkaufssignale checken nur für bestätigte Käufe, nicht für alle Empfehlungen").

## Verworfen

| Alternative | Warum verworfen |
|---|---|
| Eigene `fruehsignal_sell_flags`-Tabelle/eigenes PWA-Feld | Hätte parallele Anzeige-Logik gebraucht (zwei Bannertypen, zwei Reset-Buttons) für dasselbe Konzept "Verkaufsempfehlung" – unnötige UI-Fragmentierung |
| `sell_signal_source` auch in der PWA anzeigen | Kein Mehrwert für Josef in diesem Stadium – die Ursache steht bereits im Klartext in `sell_reason` ("Frühsignal: Insider-Verkauf ..." vs. "Sentiment gedreht"), ein zusätzliches Badge wäre redundant |

## Gilt unter

Setzt voraus, dass ein Portfolio-Eintrag zu jedem Zeitpunkt höchstens EIN aktives Verkaufssignal trägt (kein gleichzeitiges Sentiment- und Frühsignal-Sell-Flag) – strukturell erzwungen, da beide Quellen einen bereits aktiven `sell_signal=True`-Eintrag überspringen, statt ihn zu überschreiben.

## Konsequenzen

- Kein neuer PWA-Code für die Verkaufssignal-Anzeige selbst nötig, nur die Info-Sheet-Erklärung wurde ergänzt.
- Reset-Verhalten ist jetzt quellenabhängig: ein Sentiment-Sell-Flag reset sich automatisch bei guter Stimmung, ein Frühsignal-Sell-Flag braucht immer den manuellen "Signal zurücksetzen"-Button.
