# ADR-014: Automatische Vorschlags-Übernahme nur für die Basis-Schwelle, nicht für abgeleitete Zweit-Schwellen

**Datum:** 2026-08-09
**Status:** aktiv
**Projekt:** Stock Sentiment Scanner

## Problem

Josef wollte, dass die regelbasierten Analyse-Vorschläge (`_suggest_adjustments()`) automatisch in `config.json` übernommen werden ("vollautomatisch", explizit gewählt trotz Hinweis auf das Risiko bei kleiner Stichprobe). Ein `raise_threshold`-Vorschlag liefert dabei nur EINEN konkreten Zahlenwert (`suggested_value`), kann aber MEHRERE `config_keys` referenzieren – bei den Frühsignal-Hebeln (`_EARLY_LEVERS`) sind das typischerweise zwei bewusst unterschiedliche Schwellen desselben Signaltyps, z. B. `early_signals.volume_z_min` (2,5 – ab wann zählt ein Volumen-Ausschlag überhaupt als Rohsignal) und `early_signals.single_volume_z_min` (6,0 – ab wann ist dasselbe Signal allein schon stark genug für einen sofortigen Instant-Alert, ohne auf ein zweites Signal zu warten).

## Entscheidung

`_apply_suggestions()` übernimmt automatisch **nur den ersten Eintrag** von `config_keys` (die "Basis-Schwelle"). Alle weiteren Keys desselben Vorschlags werden NICHT automatisch geändert, sondern nur als `skipped_keys` auf dem Vorschlag vermerkt (sichtbar in Telegram-Alert, Analyse-Tab-Karte und Logs).

## Begründung

- Der vorgeschlagene Wert wird aus dem Mittelwert der Positiv-/Negativ-Gruppe berechnet – diese Gruppen bestehen aus bereits ausgelösten Alerts, die auf IRGENDEINEM der beiden Wege (Kombi-Alert über die Basis-Schwelle ODER Instant-Alert über die höhere Einzelsignal-Schwelle) entstanden sein können. Der eine Zahlenwert lässt sich also nicht eindeutig einem der beiden Keys zuordnen.
- Beide Keys stumpf auf denselben Wert zu setzen würde den bewusst gestalteten Sicherheitsabstand zwischen "Signal existiert" (niedrige Schwelle) und "Signal ist für sich allein schon alarmwürdig" (deutlich höhere Schwelle) einebnen – im Extremfall auf denselben Wert kollabieren lassen, was die Instant-Alert-Logik strukturell verändern würde, ohne dass das je explizit gewünscht war.
- Die Basis-Schwelle automatisch anzupassen ist dagegen unkritisch: sie steuert nur, ob ein Rohsignal überhaupt in die Datenbank geschrieben wird – ein zu niedriger/hoher Wert wird beim nächsten Analyse-Zyklus ohnehin wieder korrigiert (selbstkorrigierender Kreislauf).

## Verworfen

| Alternative | Warum verworfen |
|---|---|
| Alle config_keys automatisch auf denselben Wert setzen | Würde den Sicherheitsabstand zwischen Basis- und Einzelsignal-Schwelle stillschweigend einebnen (siehe oben) |
| Vorschlag ablehnen, solange mehr als 1 config_key betroffen ist | Hätte "vollautomatisch" für genau die Hebel ausgehebelt, bei denen die Basis-Schwelle-Übernahme unkritisch und sinnvoll ist – unnötig konservativ |
| Eigenen, proportional skalierten Wert für die zweite Schwelle berechnen (z. B. immer Faktor 2,4 wie bei den Defaults 2,5/6,0) | Zusätzliche unbelegte Modellannahme (warum genau dieser Faktor?) – nicht besser begründbar als der Status quo, eher schlechter, da es Präzision vortäuscht, die die Datenlage nicht hergibt |

## Gilt unter

Gilt für alle `_EARLY_LEVERS`-Einträge mit mehr als einem `config_key`. Betrifft aktuell `volume_z_score`, `buzz_rel_accel`, `insider_buy_total_usd` (je 2 Keys); `large_holder_pct` hat nur 1 Key und ist von dieser Einschränkung nicht betroffen. Sollte sich künftig zeigen (über die Pick-Forward-Returns, ADR-011), dass die zweite Schwelle systematisch nachgezogen werden sollte, ist das eine bewusste neue Entscheidung, keine automatische Folge dieser ADR.

## Konsequenzen

- Die Einzelsignal-Schwellen (`single_*`) driften über die Zeit NICHT automatisch mit, auch wenn die Basis-Schwelle sich durch wiederholte Übernahmen deutlich verschiebt – Josef muss sie bei Bedarf weiterhin manuell in den Einstellungen anpassen.
- Telegram-Alert und Analyse-Tab zeigen `skipped_keys` explizit an, damit dieser bewusste Nicht-Automatismus nicht als Bug missverstanden wird.
