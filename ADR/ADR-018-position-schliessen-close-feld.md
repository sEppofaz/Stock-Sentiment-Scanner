# ADR-018: "Position schließen" über neues `closed`-Feld statt eigener Tabelle/eigenem Status-Enum

**Datum:** 2026-08-21
**Status:** aktiv
**Projekt:** Stock Sentiment Scanner

## Problem

Eine Portfolio-Position ließ sich bisher nur per DELETE endgültig entfernen. Josef wollte zusätzlich eine "Schließen"-Funktion (Vorbild eToro "Geschichte des Trades"): eine verkaufte Position soll nicht aus der Historie verschwinden, sondern in einer eigenen Rubrik mit Einstand, Verkaufskurs/-datum und realisiertem Gewinn/Verlust weiter sichtbar bleiben.

## Entscheidung

Kein neues Datenmodell (keine eigene `closed_positions`-Tabelle, kein Status-Enum-Feld). Stattdessen vier neue optionale Keys direkt im bestehenden `portfolio.json`-Eintrag: `closed` (bool), `close_price`, `close_date`, `realized_pnl`, `realized_pnl_pct`. Der bestehende PATCH-Endpoint `/sentiment/api/portfolio/<ticker>` (bisher nur für `sell_signal`-Reset genutzt) bekommt einen zweiten Zweig für `{"closed": true}`. Close-Kurs ist immer der zuletzt bekannte Live-Kurs (`current_price`) – kein manueller Kurs-/Datums-Prompt.

## Begründung

- `portfolio.json` ist schema-frei (JSON-Array von Dicts, siehe `_load_portfolio()`/`_update_portfolio()` in `scanner.py`) – neue Keys sind ein reiner additiver Zusatz, keine Migration nötig.
- Direkte Fortführung von ADR-012 (Verkaufssignal-Wiederverwendung): bestehende PATCH-Route + bestehende Card-Render-Infrastruktur (`loadPortfolio()`) wiederverwenden statt neuer Endpunkte/Komponenten.
- Kein manueller Kurs-Prompt beim Schließen: Josef hatte in derselben Rückmeldung kritisiert, dass die alte `convertToReal()`-Eingabemaske aus einer Kette von vier `prompt()`-Dialogen bestand (siehe unten) – ein "Schließen"-Flow mit weiteren Prompts hätte densellben Fehler wiederholt. Der zuletzt bekannte Live-Kurs ist für einen Verkauf-zum-Marktpreis eine vertretbare Näherung (keine untertägige Präzision nötig, da der Portfolio-Scan alle 15 Min läuft).

## Verworfen

| Alternative | Warum verworfen |
|---|---|
| Eigenes `status`-Enum-Feld (`"open"`/`"closed"`) statt bool `closed` | Kein Mehrwert gegenüber einem bool, da aktuell nur zwei Zustände existieren; ein Enum würde nur für ein hypothetisches drittes Zustands-Feature vorbereiten, das nicht angefragt ist |
| Manuelle Eingabe von Schließkurs/-datum (wie bei `/convert`) | Widerspricht direkt Josefs Kritik an der prompt()-lastigen Eingabemaske (siehe ADR/Umsetzung zu Punkt "Dashboard-Eintrag-UX" in derselben Session); Live-Kurs ist ausreichend genau |
| "Wiedereröffnen" (Reopen) einer geschlossenen Position | Nicht angefragt, hätte nur Komplexität ohne aktuellen Bedarf hinzugefügt |

## Gilt unter

Setzt voraus, dass geschlossene Positionen dauerhaft vom Portfolio-Scan (`_run_portfolio_scan_inner()`, `_update_portfolio_quotes()`) und vom Frühsignal-Sell-Check (`layer6_sell_signal.check_frühsignal_sell_exits()`) ausgeschlossen bleiben (Guard `if entry.get("closed"): continue` bzw. Filter in der Listen-Comprehension) – sonst würden fixierte Werte durch neue Live-Kurse überschrieben.

## Konsequenzen

- Ein geschlossener Ticker wird von `_auto_watch()` nicht erneut als Beobachtung angelegt (Dedup-Check prüft nur Ticker-Existenz, nicht den `closed`-Status) – entspricht dem eToro-Vorbild (geschlossene Trades laufen nicht automatisch weiter), ist aber ein bewusster Kompromiss: ein neues Alert-Signal für einen bereits geschlossenen Ticker führt zu keiner neuen Beobachtung.
- Kein neuer PWA-Datenendpunkt nötig – die geschlossene Rubrik liest aus demselben `GET /sentiment/api/portfolio` und filtert clientseitig.
