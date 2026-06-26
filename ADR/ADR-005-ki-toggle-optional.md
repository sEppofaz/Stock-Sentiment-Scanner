# ADR-005: KI-Analyse als optionales Feature (ki_enabled Toggle)

**Datum:** 2026-06-26
**Status:** aktiv
**Projekt:** Stock Sentiment Scanner

## Problem

Claude-Haiku-Anreicherung lief bei jedem Vollscan automatisch mit (~0,18 € pro Scan). Die Kosten entstanden auch dann, wenn der Scan nur zur Exploration oder zum Testen gedacht war. Es gab keine Möglichkeit, einen schnellen kostenlosen Scan durchzuführen.

## Entscheidung

`ki_enabled: false` als Standard in `config.json`. Neuer Toggle „KI-Analyse (Claude) aktiv" im Einstellungen-Tab. Claude-Anreicherung läuft nur wenn explizit aktiviert.

## Begründung

- KI ist für Stimmungsentscheidungen nützlich, nicht für jeden Scan nötig
- Keyword-NLP liefert brauchbare Vorfilterung kostenlos
- Josef möchte kontrollieren wann API-Kosten entstehen
- Aligns mit dem bestehenden `scan_enabled`-Muster

## Verworfen

| Alternative | Warum verworfen |
|---|---|
| KI immer aktiv | Kosten ohne Mehrwert bei Exploration-Scans |
| KI komplett entfernen | Verliert Mehrwert bei gezielter Nutzung |
| Pro-Scan-Toggle im Dashboard | Mehr UI-Aufwand, Einstellungen-Tab passt besser |

## Gilt unter

- `config.json` ist gitignored → Einstellung bleibt beim Server-Pull erhalten
- `CLAUDE_API_KEY` muss in secrets.env gesetzt sein (bleibt Pflicht)

## Konsequenzen

- Vollscans ohne KI: ~0 € Claude-Kosten, nur Finnhub-Calls
- Vollscans mit KI: ~0,18 € (Haiku 4.5, ~100 Kandidaten)
- Info-Bereich der PWA erklärt den Toggle (Version 1.5)
