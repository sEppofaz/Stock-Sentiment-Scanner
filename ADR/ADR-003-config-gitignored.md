# ADR-003: config.json gitignored – Trennung User-Daten / Code

**Datum:** 2026-06-23
**Status:** aktiv
**Projekt:** Stock Sentiment Scanner

## Problem

`config.json` war git-tracked. Jedes `git pull` beim Deployment überschrieb die vom Benutzer gespeicherten Einstellungen (Scan-Zeiten, Filter, Top-N) mit dem Stand im Repo. Das machte die „Speichern"-Funktion der PWA wirkungslos.

## Entscheidung

`config.json` ist gitignored. `config.default.json` liegt als Vorlage im Repo. `_load_cfg()` in `app.py` kopiert `config.default.json` → `config.json` automatisch beim ersten Start, falls die Datei fehlt.

## Begründung

- User-editierbare Daten (Einstellungen) gehören nicht ins Repo — gleiches Prinzip wie `portfolio.json`, `results.json`
- `config.default.json` sichert Erstinstallation ohne manuelle Schritte
- Kein Datenverlust bei Deployment, kein manuelles Sichern vor `git pull` nötig

## Verworfen

| Alternative | Warum verworfen |
|---|---|
| config.json im Repo belassen, vor Deploy manuell sichern | Fehleranfällig, nicht automatisierbar |
| Einstellungen in DB/Redis | Massiver Overhead für eine einfache JSON-Config |
| Einstellungen in secrets.env | Falsche Abstraktionsebene – Einstellungen sind keine Secrets |

## Gilt unter

- Einstellungen werden ausschließlich über die PWA (`POST /api/config`) geändert
- Erstinstallation folgt dem Setup in CLAUDE.md (kein manuelles Anlegen von config.json nötig)

## Konsequenzen

**Positiv:** Einstellungen überleben jeden Deploy. Erstinstallation bleibt einfach.  
**Negativ:** Änderungen an Default-Werten in `config.default.json` wirken sich nicht auf bestehende Instanzen aus — muss bei Breaking-Changes kommuniziert werden.
