# ADR-016: Frankfurter.app für EUR/USD-Umrechnung, Kurs des Kaufdatums statt Eingabedatums

**Datum:** 2026-08-09
**Status:** aktiv
**Projekt:** Stock Sentiment Scanner

## Problem

Josef wollte den Kaufpreis eines Portfolio-Eintrags wahlweise in Euro angeben können (statt nur USD), mit automatischer Umrechnung. Zwei offene Fragen: (1) welcher Wechselkurs-Zeitpunkt gilt, (2) welche Datenquelle liefert den Kurs.

## Entscheidung

- **Kurs-Zeitpunkt:** Der historische EZB-Referenzkurs des **Kaufdatums** (`buy_date`), nicht des Eingabedatums. Fallback auf den aktuellen Kurs (`/latest`), falls kein Kaufdatum angegeben ist oder die historische Abfrage fehlschlägt.
- **Datenquelle:** [Frankfurter.app](https://www.frankfurter.app/) (EZB-Referenzkurse, kostenlos, kein API-Key).

## Begründung

- Der Kaufpreis soll den tatsächlich zum Kaufzeitpunkt gezahlten USD-Gegenwert widerspiegeln – gerade bei nachträglich erfassten, älteren Käufen (z.B. beim Umwandeln einer Wochen alten Auto-Watch-Beobachtung über „Zu echter Position machen") wäre der Kurs des Eingabedatums sachlich falsch.
- Finnhub (bereits im Einsatz für alle anderen Marktdaten) hat im Free-Tier keinen bekannten verlässlichen Forex-Endpoint – eine neue externe Abhängigkeit war nötig. Frankfurter.app wurde vor der Entscheidung live getestet: unterstützt native historische Datumsabfragen (`/2026-07-15?from=EUR&to=USD`), liefert an Nicht-Handelstagen automatisch den letzten verfügbaren Vortageskurs (kein Fehler), meldet nicht verfügbare/zu alte Daten sauber als `{"message":"not found"}` statt eines undefinierten Fehlers.

## Verworfen

| Alternative | Warum verworfen |
|---|---|
| Kurs des Eingabedatums (immer `/latest`) | Sachlich falsch bei nachträglich erfassten älteren Käufen – genau der Fall, für den „Zu echter Position machen" mit optionalem `buy_date` existiert |
| Finnhub-Forex-Endpoint | Kein verlässlicher Free-Tier-Zugriff bekannt, hätte vor Umsetzung erst verifiziert werden müssen; Frankfurter.app war bereits live getestet und passend |
| Eigene, selbst gehostete Kurs-Historie | Unnötiger Aufwand für eine seltene, manuelle Aktion (Kaufpreis-Eingabe) – kein Fall für eine eigene Datenhaltung |

## Gilt unter

Setzt voraus, dass Frankfurter.app/die EZB-Referenzkurs-API verfügbar bleibt (kostenlos, keine SLA). Bei Ausfall greift der eingebaute Fallback (`/latest`) bzw. bei Totalausfall eine explizite 400-Fehlermeldung statt eines stillschweigend falschen Kurses.

## Konsequenzen

- Neue externe Abhängigkeit `api.frankfurter.app` (kein API-Key, kein Kosten-Tracking nötig).
- `buy_price_eur`/`fx_rate_used` werden zusätzlich zum intern immer-USD `buy_price` gespeichert – Transparenz, welcher Kurs verwendet wurde, sichtbar auf der Portfolio-Karte.
- **Live-Lehre (eToro-Fund, PRQR-Beispiel, siehe `CLAUDE.md`):** Die Funktion selbst war korrekt, aber Broker-Apps zeigen den Stückpreis oft ohne Symbol bereits in der Handelswährung (USD) – ein Nutzer kann leicht fälschlich „EUR" wählen, obwohl der abgelesene Wert schon USD ist. Reine Funktionskorrektheit schützt nicht vor Fehleingaben durch mehrdeutige Broker-UIs – deshalb zusätzlicher Warnhinweis direkt bei der Währungsauswahl ergänzt.
