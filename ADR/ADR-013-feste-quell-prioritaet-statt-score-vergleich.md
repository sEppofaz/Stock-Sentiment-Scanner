# ADR-013: Feste Quell-Priorität statt direktem Score-Vergleich für das Tages-Pick-Ranking

**Datum:** 2026-08-09
**Status:** aktiv
**Projekt:** Stock Sentiment Scanner

## Problem

Der ursprüngliche Layer-6-Plan sortierte Kandidaten unterschiedlicher Quellen direkt nach ihrem numerischen Score gegeneinander, um den einen Tages-Pick zu bestimmen. Die Score-Skalen sind aber strukturell nicht vergleichbar: Frühsignal-Scores (additive Gewichte aus `layer4_scoring.py`, insider 3+2 Cluster, volume 2-3, buzz 1, large_holder 1.5-3) bewegen sich in einer Spanne von ca. 2-10, während der Sentiment-Scan-Score (`scanner._calc_score()`, 45% Bullish + 30% Buzz + 25% NLP-Score) auf einer 0-100-Skala liegt. Ein direkter Vergleich hätte de facto immer den Sentiment-Scan-Kandidaten bevorzugt, unabhängig von der tatsächlichen Signalqualität – exakt das Problem, das die ursprüngliche Fable-Review am Gesamt-Scoring bereits kritisiert hatte, hier nur eine Ebene höher (bei der Konsolidierung) wieder eingebaut.

## Entscheidung

Kein direkter Score-Vergleich zwischen Quellen. Stattdessen eine feste Prioritätsreihenfolge: `cross_signal` (Bestätigung durch beide Systeme) > `early_signals` > `sentiment_scan`. Innerhalb derselben Quelle wird weiterhin nach dem jeweils eigenen Score sortiert (Vergleich bleibt innerhalb derselben Skala, dort ist er sinnvoll). Zusätzlich ein absoluter Mindest-Score je Quelle (`daily_pick.min_score_early_signals`/`min_score_sentiment_scan`), damit auch der jeweils beste Kandidat eines Tages eine Mindestqualität erreichen muss – sonst "kein Pick" statt eines erzwungenen schwachen Picks.

## Begründung

- `cross_signal` als höchste Priorität ist inhaltlich begründbar (zwei unabhängige Systeme bestätigen denselben Ticker – stärkeres Signal als jede Einzelquelle), nicht nur eine willkürliche Tie-Breaker-Regel.
- Ein fest priorisiertes Ranking ist nachvollziehbar und im `reasoning_json` transparent erklärbar ("Quelle: cross_signal"), ein normierter Misch-Score wäre eine weitere unbelegte Kalibrierungsannahme obendrauf.
- Der von Fable gefundene Fehler war real und live im Code verifizierbar (keine Stilkritik) – die Konsequenz (feste Priorität statt Vergleich) ist die direkte, minimale Korrektur.

## Verworfen

| Alternative | Warum verworfen |
|---|---|
| Score-Normierung (z.B. beide Skalen auf 0-100 mappen) | Bräuchte eine kalibrierte, empirisch hergeleitete Umrechnung – bei aktuell noch kleiner Datenbasis (siehe `weekly_analysis.py` MIN_SAMPLE-Problematik) nicht seriös herleitbar, nur eine neue Vermutung anstelle der alten |
| Gewichteter Summen-Score über beide Quellen | Löst das Grundproblem nicht, verschiebt es nur um eine Stufe (die Gewichte selbst wären wieder unbelegt) |
| Direkter Score-Vergleich beibehalten (ursprünglicher Plan) | Bevorzugt strukturell immer die höhere Skala unabhängig von der tatsächlichen Signalstärke – der von Fable gefundene Bug |

## Gilt unter

Gilt so lange, wie keine empirisch belastbare Kalibrierung zwischen den beiden Score-Systemen vorliegt (vgl. `weekly_analysis.py`, `_suggest_adjustments()`). Sollte über mehrere Monate genug Pick-Historie mit Forward-Returns vorliegen, um zu prüfen, ob z.B. `sentiment_scan`-Picks systematisch schlechter performen als `early_signals`-Picks, kann diese Priorität revidiert oder durch eine datenbasierte Gewichtung ersetzt werden.

## Konsequenzen

- Ein `sentiment_scan`-Kandidat mit sehr hohem Score kann von einem `early_signals`-Kandidaten mit niedrigerem (aber über der Mindestschwelle liegendem) Score verdrängt werden – gewollt, da die Quellen nicht direkt vergleichbar sind.
- Zwei zusätzliche Config-Werte (`min_score_early_signals`/`min_score_sentiment_scan`) müssen gepflegt/kalibriert werden, sobald mehr Erfahrungswerte vorliegen.
