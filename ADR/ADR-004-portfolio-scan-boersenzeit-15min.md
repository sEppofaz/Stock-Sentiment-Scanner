# ADR-004: Portfolio-Scan alle 15 Min mit Börsenzeit-Guard

**Datum:** 2026-06-25
**Status:** aktiv
**Projekt:** Stock Sentiment Scanner

## Problem

Portfolio-Scan lief stündlich Mo–Fr 9:30–22:30 UTC – also auch außerhalb der NYSE-Börsenzeiten (14:30–21:00 UTC). Gleichzeitig war die Frequenz für Sell-Signal-Erkennung zu grob.

## Entscheidung

Portfolio-Scan läuft alle 15 Min (APScheduler `minute="0,15,30,45", hour="14-21"`), aber nur wenn `_market_open()` True ist (14:30–21:00 UTC). Der Guard ist eine reine Python-Funktion in app.py, kein separater APScheduler-Job.

## Begründung

- Sell-Signale sollen zeitnah erkannt werden → 15 Min Takt sinnvoll
- Finnhub-Calls pro Scan minimal (2 Calls/Aktie) → kein Free-Tier-Problem
- Außerhalb Börsenzeiten keine Kurs-/Sentiment-Änderungen → Scans wären Leerlauf
- Ein einziger APScheduler-Job + Guard ist einfacher zu warten als mehrere Jobs mit exakten Börsenzeiten-Grenzen

## Verworfen

| Alternative | Warum verworfen |
|---|---|
| Drei separate APScheduler-Jobs (14:30–14:45, 15:00–20:45, 21:00) | Komplex, fehleranfällig bei Sommerzeit-Grenzen |
| Stündlicher Takt beibehalten | Zu grob für zeitnahe Sell-Alerts |
| Scan auch außerhalb Börsenzeiten | Sinnlos – Kurse und News ändern sich nicht |

## Gilt unter

- NYSE/NASDAQ-Öffnungszeiten bleiben 14:30–21:00 UTC (Normalzeit). Bei US-Sommerzeit ggf. anpassen.
- Portfolio-Größe bleibt überschaubar (< 20 Aktien) → Finnhub-Limit kein Problem

## Konsequenzen

- Sell-Alerts kommen maximal 15 Min nach Stimmungsdrehung (statt bis zu 60 Min)
- Telegram-Alert bleibt nur bei echter Stimmungsdrehung (5-Punkte-Buffer unverändert)
- Ca. 28 Scans/Handelstag statt 14 → doppelte Portfolio-Scan-Calls (bei 5 Aktien: ~280 Calls/Tag – vernachlässigbar)
