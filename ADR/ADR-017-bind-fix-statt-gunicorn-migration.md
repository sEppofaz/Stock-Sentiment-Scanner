# ADR-017: Nur Bind-Fix (127.0.0.1) statt vollem gunicorn-Umzug

**Datum:** 2026-08-21
**Status:** aktiv
**Projekt:** Stock Sentiment Scanner

## Problem

Beim Nachziehen der Claude-Remote-Whitelist (PKA-Session 2026-08-21) fiel auf, dass `app.run(host="0.0.0.0", port=5005, ...)` auf allen Netzwerk-Interfaces lauschte statt nur auf `127.0.0.1` – abweichend vom Muster aller anderen Flask-Apps auf dem Server (gunicorn, `127.0.0.1`-Bindung, nginx als einziger Weg rein). Nur durch UFW-Default-Deny auf Port 5005 abgesichert (verifiziert), kein explizites Nginx-only-Pattern.

## Entscheidung

Nur die Bindung auf `127.0.0.1` ändern (1 Zeile), weiterhin über Flasks eingebauten Dev-Server statt gunicorn.

## Begründung

Josef explizit gefragt (VT/NT-Abwägung im Chat) – Minimalfix ist praktisch risikofrei und schließt die eigentliche Lücke (Erreichbarkeit von außen) sofort. Der volle gunicorn-Umzug hätte echten Zusatzaufwand *und* ein reales Risiko: Die App hat einen eingebauten `apscheduler` (Portfolio-Scan alle 15 Min, Daily-Pick, Sell-Signal-Check, siehe `ADR-004`). Gunicorns Standard-Mehrfach-Worker würden den Scheduler mehrfach parallel laufen lassen → doppelte/mehrfache Scans, doppelte Finnhub-/Claude-API-Calls, potenziell doppelte Telegram-Alerts.

## Verworfen

| Alternative | Warum verworfen |
|---|---|
| Sofortiger vollständiger gunicorn-Umzug (wie Claude-Remote/rename-webhook) | Bräuchte zusätzlich `-w 1` (Single-Worker, wegen Scheduler) oder eine Auslagerung des Schedulers in einen separaten Prozess – mehr Testaufwand für eine bereits laufende Produktiv-App, ohne dass akuter Zeitdruck bestand (Firewall blockte externen Zugriff bereits) |
| Nichts tun (Firewall reicht als Schutz) | Kein explizites Nginx-only-Pattern wie bei allen anderen Apps – Defense-in-Depth-Lücke bliebe bestehen, obwohl der Fix trivial ist |

## Gilt unter

Gilt solange die App als Single-Instance mit dem eingebauten `apscheduler` läuft. Wird hinfällig, sobald jemand den gunicorn-Umzug tatsächlich umsetzt (dann muss die Scheduler-Frage aus diesem ADR mit gelöst werden, nicht vergessen).

## Konsequenzen

- App läuft weiterhin über Flasks Dev-Server (Warnung im eigenen Log bleibt bestehen) – funktional unverändert, nur nicht mehr von außen erreichbar.
- Offener PKA-Todo für den vollen gunicorn-Umzug inkl. Scheduler-Lösung, kein Zeitdruck.
