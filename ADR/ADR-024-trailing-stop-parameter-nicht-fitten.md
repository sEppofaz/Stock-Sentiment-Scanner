# ADR-024: Trailing-Stop-Parameter bleibt bei 15% – keine Anpassung auf Basis der aktuellen Stichprobe

**Datum:** 2026-09-27
**Status:** aktiv
**Projekt:** Stock Sentiment Scanner

## Problem

Fable-Analyse 2026-09-11, Empfehlung (4): „Exit-/Risikomanagement (Trailing-Stop-Parameter) verschärfen statt weiter an Einstiegssignalen drehen – der Hebel mit der besten Aussicht auf Expectancy-Verbesserung ohne neue statistische Unsicherheit." Der Default in ADR-020 (15% pro Position) war nie gemessen, sondern gesetzt. Frage: Lässt sich ein besserer Wert aus den vorhandenen Daten ableiten?

## Entscheidung

**Der Default bleibt bei 15%.** Statt einer Parameteranpassung wird die Messgrundlage als wiederholbares Skript ins Repo aufgenommen (`ts_backtest.py`) und quartalsweise erneut ausgeführt. Eine Anpassung erfolgt erst, wenn die Stichprobe mehr als ein Marktregime abdeckt.

## Begründung

Der Backtest (n=207 ausgereifte, deduplizierte `insider_buy`-Fälle, Zeitraum 2026-07-06 bis 2026-08-26, Horizont 20 Handelstage) sieht auf den ersten Blick eindeutig aus – je enger der Stop, desto besser:

| Variante | Trefferquote | Median | Expectancy | gestoppt |
|---|---|---|---|---|
| Halten | 44,9% | −0,96% | −0,14% | 0% |
| TS 5% | 62,8% | +1,20% | **+1,71%** | 94% |
| TS 8% | 54,6% | +0,39% | +0,74% | 83% |
| TS 10% | 46,4% | −0,47% | +0,49% | 73% |
| TS 12% | 45,4% | −0,99% | +0,04% | 65% |
| TS 15% (heute) | 41,5% | −1,50% | −0,63% | 52% |
| TS 20% | 42,5% | −1,50% | −1,68% | 33% |

Zwei Gegenprüfungen zerlegen dieses Ergebnis:

1. **Zeitstabilität.** Bei Halbierung der Stichprobe nach Signaldatum kippt die Rangfolge vollständig. Erste Hälfte (06.07.–11.08., n=103): **Halten ist mit +1,54% die beste Variante**, TS 15% liegt mit +0,65% vor TS 10%/12% (beide ≈0%). Zweite Hälfte (11.08.–26.08., n=104): Halten ist mit −2,74% die schlechteste, TS 5% mit +1,33% die beste. Der gepoolte Befund beschreibt damit das Marktregime des zweiten Zeitfensters, nicht die Exit-Regel.
2. **Slippage.** Die engen Stops lösen fast immer aus (TS 5%: 94% der Fälle), also trifft sie jeder Prozentpunkt Ausführungsabschlag praktisch bei jeder Position. Auf Microcaps zwischen 50 Mio. und 2 Mrd. $ Marktkapitalisierung sind 1–2% Spread realistisch. Damit schmilzt TS 5% von +1,71% auf +0,91% (1%) bzw. +0,18% (2%), TS 8% von +0,74% auf −0,54%, TS 10% auf −0,97%.

Ein Wechsel von 15% auf 5–8% wäre also eine Anpassung an sieben Wochen fallenden Markt, deren gemessener Vorsprung bei realistischer Ausführung fast vollständig verschwindet. Das ist genau die Art Überanpassung, vor der die Fable-Analyse an anderer Stelle gewarnt hat – nur an einem anderen Parameter.

## Verworfen

| Alternative | Warum verworfen |
|---|---|
| Default auf 5% senken (bester gepoolter Wert) | Löst in 94% der Fälle aus, mittlere Haltedauer 4,5 Handelstage. Bei 1–2% Slippage bleibt fast nichts übrig; in der ersten Stichprobenhälfte war es zudem schlechter als Halten. Würde aus einem Positions-Stop faktisch einen Daytrading-Mechanismus machen. |
| Default auf 8–10% senken (Kompromiss) | Schon bei 1% Slippage negativ (−0,54% / −0,27%), in der ersten Stichprobenhälfte ohne Vorteil gegenüber 15%. Wirkt wie eine Verbesserung, ist aber reines Kurvenfitting. |
| Trailing-Stop global in `config.json` statt pro Position | Bereits in ADR-020 entschieden (Risikotoleranz ist positionsabhängig). Unverändert gültig. |
| Zusätzlich einen Zeit-Stop einführen („nach N Tagen ohne Fortschritt raus") | Verschiebt das Fitting-Problem nur auf einen neuen Parameter N, der aus derselben zu kleinen Stichprobe geschätzt werden müsste. |
| Ergebnis gar nicht dokumentieren, Todo offen lassen | Die Messung ist das eigentliche Ergebnis – ohne sie würde die Frage in jeder künftigen Session erneut gestellt. |

## Gilt unter

- Die Stichprobe umfasst ein einziges Marktregime (Sommer 2026, überwiegend fallende Small Caps) und ~7 Wochen je Hälfte.
- Slippage-Annahme 1–2% für Microcaps zwischen 50 Mio. und 2 Mrd. $ Marktkapitalisierung.
- Ungültig, sobald `ts_backtest.py` über einen Zeitraum mit mindestens einer deutlichen Aufwärtsphase läuft **und** die Rangfolge dann in beiden Stichprobenhälften stabil bleibt. Dann ist eine Parameteränderung datengestützt möglich.

## Konsequenzen

- **Positiv:** Die Frage ist beantwortet statt wiederholt aufgeworfen. `ts_backtest.py` liefert die Antwort künftig in 3–5 Minuten Laufzeit, kostenlos und ohne Eingriff in `signals.db`.
- **Positiv:** Nebenbefund als Bestätigung von ADR-022: dieselbe Auswertung zeigt für die reine `insider_buy`-Gruppe Expectancy +0,71% (n=54, Stand 27.09.), für `insider_buy` in Kombination mit anderen Typen aber −1,70% (n=122) und für `volume_anomaly` allein −14,13% (n=175). Die additive Kombination verwässert messbar.
- **Negativ:** Positionen laufen weiter mit einem nicht optimierten Stop. Bei der gemessenen Expectancy von ≈0 ist das allerdings kein bezifferbarer Verlust gegenüber einer Alternative.
- Die Fable-Empfehlung (4) gilt damit als **geprüft und vorläufig nicht umsetzbar**, nicht als umgesetzt.
