# ADR-002: Claude Haiku als zweite Sentiment-Analysestufe

**Datum:** 2026-06-23
**Status:** aktiv
**Projekt:** Stock Sentiment Scanner

## Problem

Das bestehende Keyword-Scoring (BULLISH_WORDS / BEARISH_WORDS) ist eine grobe Annäherung: Es erkennt keine Ironie, keinen Kontext und keine implizite Stimmung. Kandidaten werden teils falsch klassifiziert (z.B. „Apple beats expectations but warns of slowing growth" → netto positiv durch „beats", obwohl ein Warning drin ist).

## Entscheidung

Claude Haiku 4.5 analysiert die Kandidaten-Ticker (nach dem Keyword-Vorfilter) in 10er-Batches auf Basis ihrer Nachrichtentexte. Die Keyword-Scores werden durch Claude-Scores ersetzt. Kosten werden pro Scan und kumulativ in `claude_costs.json` gespeichert, Telegram-Alert bei neuem €1-Schwellenwert.

## Begründung

- Keyword-NLP als Vorfilter bleibt sinnvoll: spart 95%+ der Claude-Calls (4700 → ~50–150 Kandidaten)
- Haiku 4.5 ist günstig genug ($1/$5 per 1M Tokens) → ~$0.01–0.05 pro Vollscan
- Graceful Fallback: ohne `ANTHROPIC_API_KEY` läuft Keyword-NLP weiter (Deployments ohne neuen Key nicht blockiert)
- ANTHROPIC_API_KEY war bereits in `/etc/pka/secrets.env` vorhanden

## Verworfen

| Alternative | Warum verworfen |
|---|---|
| Claude für alle 4700 Ticker | 10–40x teurer, ~$0.50–2.00/Scan; API-Throttling-Risiko während 90-Min-Scan |
| Claude Sonnet/Opus statt Haiku | Sentiment-Klassifikation ist keine komplexe Reasoning-Aufgabe; Haiku ausreichend |
| Externes NLP (VADER, spaCy) | Kein signifikanter Qualitätsvorteil gegenüber Keyword-Scoring; mehr Abhängigkeiten |
| Finnhub Premium `/news-sentiment` | Kostenpflichtig (freier Tier gibt 403); kein Mehrwert wenn Claude verfügbar |
| Eigene Fine-Tuning-Modell | Viel zu aufwändig für diesen Use Case |

## Gilt unter

- Haiku 4.5 bleibt verfügbar und bezahlbar (Preisänderung würde Neubewertung erfordern)
- Kandidaten-Menge bleibt im Bereich 50–300 pro Scan (bei Explosion → Batching anpassen oder Stufe einengen)
- `ANTHROPIC_API_KEY` in `/etc/pka/secrets.env` gesetzt

## Konsequenzen

**Positiv:**
- Deutlich bessere Sentiment-Qualität für die finale Top-50-Liste
- Kosten vollständig nachvollziehbar und transparent in PWA + Telegram
- Keyword-NLP bleibt als schneller Grobfilter erhalten

**Negativ:**
- Scan dauert marginal länger (Claude-Batch-Calls nach Stufe 1)
- Externe Abhängigkeit von Anthropic API hinzugekommen
- Bei Claude-API-Ausfall: Fallback auf Keyword-Scores (akzeptabler Degradationsmodus)
