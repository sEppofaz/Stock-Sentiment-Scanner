# ADR-001: Sentiment-Quelle /company-news + Keyword-NLP statt /news-sentiment

**Datum:** 2026-06-23
**Status:** aktiv
**Projekt:** Stock Sentiment Scanner

## Problem

Der Scanner war ursprünglich auf den Finnhub-Endpoint `/news-sentiment` ausgelegt, der fertig berechnete Sentiment-Werte (bullishPercent, bearishPercent, buzz, companyNewsScore) liefert. Dieser Endpoint gibt im Finnhub Free Tier 403 zurück – alle 4.723 Ticker schlugen beim ersten vollständigen Scan fehl, Ergebnisliste war leer.

## Entscheidung

Umstieg auf Finnhub `/company-news` (Free Tier) + eigenes Keyword-Scoring:
- Artikel der letzten 7 Tage pro Ticker holen
- Headline + Summary per Keyword-Sets analysieren (BULLISH_WORDS / BEARISH_WORDS in `scanner.py`)
- Buzz = Artikelanzahl / 3,0 (3 Artikel/Woche = 1,0 = „normal")
- Bullish % = Anteil Artikel mit positivem Keyword-Score
- Bearish % = Anteil Artikel mit negativem Keyword-Score

## Begründung

- `/company-news` ist im Finnhub Free Tier verfügbar und bestätigt (HTTP 200)
- Identische API-Call-Anzahl wie vorher (1 Call pro Ticker) → keine Änderung an Scan-Dauer oder Rate-Limiting
- Grundhypothese des Scanners (Buzz-Anstieg als Frühsignal) bleibt vollständig erhalten
- Keyword-Scoring ist eine Annäherung, aber für den explorativen Ansatz ausreichend

## Verworfen

| Alternative | Warum verworfen |
|---|---|
| Finnhub Premium ($49–99/Mon) | Laufende Kosten für explorativen Ansatz nicht gerechtfertigt |
| Alpha Vantage News Sentiment API | Free Tier: nur 25 Requests/Tag – für 4.700 Ticker unbrauchbar; Premium: $50+/Mon |
| Polygon.io | Free Tier: 5 Calls/Min – Scan würde ~16h dauern |
| yfinance + manuelle News-Analyse | Keine strukturierten Sentiment-Daten, hoher Aufwand |

## Gilt unter

- Finnhub `/company-news` bleibt im Free Tier verfügbar
- Keyword-Listen (BULLISH/BEARISH_WORDS) decken den relevanten Finanz-Vokabular ausreichend ab
- Überprüfungswürdig wenn: Ergebnisqualität (Precision/Recall) nach mehreren Scans unbefriedigend erscheint

## Konsequenzen

**Positiv:**
- Kostenlos, sofort funktionsfähig
- Buzz-Signal (Artikelanzahl) bleibt erhalten – die eigentliche Kernhypothese

**Negativ:**
- Keyword-Scoring ist weniger präzise als Finnhubs NLP (kein Kontext, keine Negation)
- Buzz nicht mehr gegen Jahresdurchschnitt normiert, sondern gegen fixen Schwellwert (3 Artikel/Woche)
- Ironie/Sarkasmus in Artikeln wird falsch klassifiziert
