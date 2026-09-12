# ADR-022: Nur noch insider_buy + large_holder als aktionables Kaufsignal

**Datum:** 2026-09-12
**Status:** aktiv
**Projekt:** Stock Sentiment Scanner

## Problem

Eine Payoff-Ratio/Expectancy-Analyse (2026-09-11, ausgelöst durch Josefs Frage „wenn wir unter 70% Trefferquote sind, brauchen wir einen ganz neuen Ansatz") zeigte: Das Gesamtsystem hat negative Expectancy – Sentiment-Scan −3,92%/Position, Frühsignale gesamt −3,79%, `volume_anomaly` isoliert −7,43%. Einzige Gruppe mit sowohl Trefferquote >50% als auch Payoff-Ratio >1: reine Insider-Käufe (n=35, Payoff-Ratio 1,30, Expectancy +1,69%). 13D/13G-Großaktionärsmeldungen haben noch 0 gereifte Datenpunkte, sind aber mechanistisch ähnlich fundiert (echter Eigentümer-Informationsvorsprung, akademisch gut belegter Insider-Trading-Effekt).

Fable (unabhängige Analyse) bestätigte: `volume_anomaly`/`buzz_accel`/Sentiment-Scan sollten nicht mehr als eigenständiger Kaufanlass gelten; additive Kombination verwässert das stärkste Signal (Insider+Volumen performte schlechter als Insider allein) eher, als sie zu verstärken.

## Entscheidung

Zwei neue Config-Keys steuern zentral, welche Signale Alert/Daily-Pick/Auto-Watch auslösen dürfen:

- `early_signals.actionable_types` (Default `["insider_buy", "large_holder"]`) – in `layer4_scoring.py` (`run_scoring()`, `check_instant_alerts()`) und `layer6_daily_pick.py` (`_distinct_signal_types_recent()`).
- `daily_pick.sentiment_scan_actionable` (Default `false`) – schaltet den Sentiment-Scan-Snapshot-Pfad (C2/C3) in `layer6_daily_pick.py` global ab; Cross-Signal-Logik (braucht zwingend einen Snapshot-Kandidaten) wird dadurch automatisch inert statt kaputt.

Die Rohsignal-Erzeugung (`layer1_edgar.py`, `layer2_volume.py`, `layer3_buzz.py`, `layer5_ownership.py`, `scanner.py`-Sentiment-Scan) bleibt **unverändert aktiv** – Scans laufen für eine spätere Neubewertung weiter, nur die Bewertungsschicht filtert. PWA zeigt deaktivierte Signale weiter an, kennzeichnet sie aber als „⏸ nicht mehr aktionabel".

## Begründung

- **Config-getrieben statt hart verdrahtet:** Beide Werte werden überall per `.get(key, default)` gelesen – reversibel ohne Code-Änderung, falls sich das Bild mit mehr Daten (insb. 13D/13G, aktuell n=0) wieder ändert. Passt zum etablierten Kill-Switch-Muster (`scan_enabled`, `ki_enabled`, `trailing_stop.enabled`).
- **Scans bewusst nicht abgeschaltet:** Josefs explizite Vorgabe – Datensammlung ist kostenlos (Finnhub/yfinance) und die einzige Möglichkeit, die Entscheidung später mit mehr Daten zu überprüfen.
- **13D/13G bewusst nicht ausgeschlossen:** Josef nannte explizit nur Sentiment-Scan/Volumen/Buzz; 13D/13G ist mechanistisch ähnlich fundiert wie Insider-Käufe, nur unbewiesen mangels Reife.
- **Keine Server-Config-Migration nötig:** Live verifiziert – das gitignored `config.json` auf dem Server hatte die neuen Keys nach dem Deploy nicht, die Fallback-Defaults griffen ohne manuellen Eingriff.
- **Nebenbei gefixt:** `saveConfig()` in der PWA baute den `early_signals`-Block ohne `..._cfg.early_signals`-Spread neu auf (Regression des bereits 2026-07-07 gefixten Bug-Typs) – ein server-seitig gesetztes `actionable_types` wäre beim nächsten Speichern im Einstellungen-Tab verworfen worden. Direkt mitgefixt, da er die Reversibilität dieser Entscheidung technisch untergraben hätte.

## Verworfen

| Alternative | Warum verworfen |
|---|---|
| Hart im Code verdrahtet (Signal-Typen direkt in `if`-Bedingungen ausschließen) | Nicht reversibel ohne erneuten Deploy – widerspricht der Vorgabe, dass die Entscheidung provisorisch ist |
| Sentiment-Scan/`volume_anomaly`/`buzz_accel`-Scans komplett abschalten | Josefs explizite Vorgabe: Datensammlung soll weiterlaufen für eine spätere Neubewertung, kostet nichts |
| Nur `insider_buy` zulassen, 13D/13G ebenfalls ausschließen | Nicht von Josef verlangt; 13D/13G mechanistisch ähnlich fundiert wie Insider-Käufe, nur unbewiesen mangels Daten |
| Betroffene PWA-Bereiche (Dashboard, Signal-Feed-Chips) komplett entfernen | Josefs Vorgabe: weiterhin anzeigen, nur klar als „nicht mehr aktionabel" kennzeichnen – Transparenz/Nachvollziehbarkeit |
| Komplettes Neubauen der App statt Modifikation | Verworfen nach direkter Rückfrage – bestehende Datenpipeline (yfinance/EDGAR-Pitfalls, Login, atomare Writes) und `signals.db`-Historie sind der eigentliche Wert; der tatsächliche Änderungsumfang ist klein (2 Backend-Dateien + PWA-Kennzeichnung) |

## Gilt unter

Setzt voraus, dass die Payoff-Ratio-Analyse vom 2026-09-11 weiterhin die beste verfügbare Evidenz ist. Sollte der Insider-Only-Bucket (aktuell n=35, 95%-CI ±16pp) mit mehr Daten (Ziel n>100, ca. 9–12+ Monate bei aktuellem Form-4-Tempo) die positive Expectancy nicht bestätigen, oder sollten 13D/13G-Daten reifen und ein anderes Bild zeigen, ist diese Entscheidung zu überprüfen – dafür existieren die beiden Config-Keys.

## Konsequenzen

- Deutlich weniger Alerts/Daily-Picks/Auto-Watch-Einträge als zuvor (nur noch insider_buy/large_holder-basiert) – erwartet und gewollt, nicht als Bug zu werten.
- `_SOURCE_PRIORITY`, `_has_cross_signal()` in `layer6_daily_pick.py` bleiben im Code stehen, sind aber inert, solange `sentiment_scan_actionable=false` – bei künftigen Refactorings nicht versehentlich als toten Code entfernen.
- `single_volume_z_min`/`single_buzz_accel_min` in `config.json`/PWA-Einstellungen sind aktuell wirkungslos (Hinweistext ergänzt), aber bewusst nicht entfernt – geringste Störung bei einer möglichen späteren Reaktivierung.
- PWA-Version 1.29 → 1.30. Live-Verifikation im Browser stand bei Deploy noch aus (Chrome-Extension nicht verbunden) – PKA-Todo angelegt.
