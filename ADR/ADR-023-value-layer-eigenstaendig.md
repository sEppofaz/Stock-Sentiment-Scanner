# ADR-023: Value-Layer als eigenständiger Signaltyp, kein Combo-Scoring

**Datum:** 2026-09-12
**Status:** aktiv
**Projekt:** Stock Sentiment Scanner

## Problem

Josef fragte nach Ideen, wie man Aktien bewertet oder automatisiert Firmen findet, die fundamental unterbewertet sind, aber großes Potential haben — komplementär zum bestehenden Sentiment-/Frühsignal-System, das auf News-Buzz, Insider-Käufen, Volumen-Anomalien und Großaktionärsmeldungen basiert. Zwei Architekturfragen mussten geklärt werden: (1) Wie tief lässt sich mit dem kostenlosen Finnhub-Datenzugang überhaupt eine Fundamentalbewertung (KGV, ROIC, EV/EBITDA, Piotroski-F-Score, Altman-Z-Score) umsetzen? (2) Soll ein neuer Signaltyp in das bestehende Kombinations-Scoring (`layer4_scoring.py`) einfließen oder eigenständig bleiben?

## Entscheidung

1. **Eigener Layer, eigener Signaltyp `"value"`** in der bestehenden generischen `signals`-Tabelle (kein Schema-Change). Rang-basiertes Scoring: Value-Rang (KGV, P/B, EV/FCF) + Quality-Rang (ROE, Current Ratio, Debt/Equity, Umsatzwachstum), je 0–100-Perzentil über die gefilterte Kandidatenmenge, 50/50 kombiniert — statt additiver Bonuspunkte wie beim bestehenden KGV-Bonus in `scanner._calc_score()`.
2. **Kein Combo-Scoring mit Sentiment/Frühsignalen.** `"value"` wird NICHT zu `early_signals.actionable_types` hinzugefügt und fließt nicht in `layer4_scoring.py` ein. Eigener Tab, eigener Endpoint, eigener (noch zu bauender) Langzeit-Tracker.
3. **Feldsubstitutionen nach Live-Verifikation gegen Finnhub Free Tier** (AAPL/F/KSS/PLUG/GPRO, `/stock/metric`): `roicTTM` existiert nicht → ROE als primäre Profitabilitäts-Kennzahl statt ROIC. `evEbitda` existiert nicht → EV/FCF (`currentEv/freeCashFlowTTM`) statt EV/EBITDA. Kein voller Piotroski-F-Score/Altman-Z-Score möglich (keine YoY-Zeitreihe, keine rohen Bilanzgrößen im Free Tier) → reduzierter F-Score-Proxy (transparent als `(erreichte, verfügbare)` Kriterien ausgegeben) + grobes Distress-Veto statt vorgetäuschter Kennzahlen.
4. **Scheduler-Slot: wöchentlich Samstag 06:00 UTC**, nicht im werktäglichen 15-Minuten-Raster der übrigen Layer.

## Begründung

- **Direkte Lehre aus ADR-022 (gleicher Tag):** Eine Payoff-Ratio-Analyse zeigte dort, dass additive Kombination mehrerer unbewiesener Signaltypen die Expectancy verschlechtert statt verbessert (Insider+Volumen schlechter als Insider allein). Ein fundamental andersartiges, komplett unbewiesenes Signal (Value/Quality) direkt in dieselbe Bewertungsschicht wie Insider-Käufe zu mischen, würde denselben Fehler wiederholen, bevor überhaupt eigene Evidenz vorliegt.
- **Andere Zeitskala:** Frühsignale zielen auf Tage (1/5/20 Handelstage Forward-Tracking), Value-Picks brauchen laut Value-Investing-Literatur typischerweise Monate bis Jahre, bis sich eine Unterbewertung auflöst. Ein gemeinsamer Score würde zwei nicht vergleichbare Zeithorizonte vermischen.
- **Ehrlichkeit vor Vollständigkeit bei den Kennzahlen:** Ein geratener/approximierter Piotroski-Score oder Altman-Z-Score aus unpassenden Ersatzgrößen würde eine Genauigkeit vortäuschen, die nicht da ist. Reduzierte, transparent gekennzeichnete Kennzahlen (EV/FCF statt EV/EBITDA, ROE statt ROIC, `f_score_reduced` mit Verfügbarkeits-Angabe) sind ehrlicher und im konkreten Test (KSS/M/BBY/T/INTC vorn, AAPL trotz Top-ROE hinten wegen hoher Bewertung) auch fachlich plausibel.
- **Samstags-Slot vermeidet Throttle-Konflikt strukturell statt nur zu mildern:** Ein Scan über alle ~4.700 Ticker braucht beim gemeinsamen 55-Calls/Min-Finnhub-Throttle selbst ~85 Minuten. Alle bestehenden Finnhub-Jobs laufen `mon-fri` — ein Samstags-Slot braucht keine Minuten-Staffelung wie bei den werktäglichen 15-Min-Jobs, weil dort schlicht kein anderer Job um dasselbe Budget konkurriert. Fundamentaldaten ändern sich zudem nicht untertägig, ein wöchentlicher Rhythmus ist inhaltlich ausreichend.
- **Manueller Trigger umgeht bewusst den `enabled`-Kill-Switch** (analog `run_daily_pick_manual`), damit ohne Config-Änderung/Warten auf Samstag getestet werden kann — Testbarkeit ohne Risiko für den produktiven Automatik-Pfad.

## Verworfen

| Alternative | Warum verworfen |
|---|---|
| Value-Signal in `early_signals.actionable_types`/`layer4_scoring.py` einbeziehen | Direkte Wiederholung des ADR-022-Fehlers (additive Kombination unbewiesener Signale verschlechtert Expectancy) — ohne eigene Evidenz nicht zu rechtfertigen |
| Vollständiger Piotroski-F-Score / Altman-Z-Score mit geschätzten/genäherten Eingangsgrößen | Würde eine methodische Genauigkeit vortäuschen, die die verfügbaren Finnhub-Free-Tier-Daten nicht hergeben (keine Zeitreihe, keine rohen Bilanzgrößen) |
| Value-Scan im werktäglichen 15-Min-Raster mit Minuten-Staffelung wie die übrigen Frühsignal-Jobs | Unnötig kompliziert — ein exklusiver Wochenend-Slot löst den Throttle-Konflikt strukturell, Fundamentaldaten brauchen ohnehin keine untertägige Aktualisierung |
| Eigenes, neues Projekt/eigene App statt Layer im bestehenden Sentiment Scanner | Josef-Entscheidung nach Rückfrage: bestehende Finnhub-Anbindung, Scheduler, PWA-Infrastruktur und `signals.db`-Historie sind wiederverwendbar, tatsächlicher Zusatzaufwand als Layer klein |

## Gilt unter

Setzt voraus, dass Finnhub Free Tier die verifizierten Felder (KGV, P/B, ROE, Debt/Equity, Current Ratio, EV/FCF, Umsatzwachstum) weiterhin liefert. Die Entscheidung gegen Combo-Scoring gilt, bis eine eigene Payoff-/Expectancy-Analyse des Value-Layers (analog der Frühsignal-Analyse vom 2026-09-11) über einen ausreichend langen Zeitraum (Value-Picks brauchen Monate) zeigt, ob und wie er mit den bestehenden Signalen zusammenspielt — dafür ist der geplante Langzeit-Tracker (`value_tracker.py`, Horizonte 60/120/250 Handelstage, noch offen als Todo) Voraussetzung.

## Konsequenzen

- Neuer, komplett unabhängiger Pfad (`layer7_value.py`, eigener Tab, eigener Endpoint) — keine Rückwirkung auf bestehende Alert-/Daily-Pick-/Auto-Watch-Logik.
- Erste aussagekräftige Auswertung erst nach Monaten möglich (Langzeit-Tracker noch nicht gebaut, PKA-Todo).
- `value_layer.enabled` als Kill-Switch (Default `false`, am 2026-09-12 nach Aufbau + Tests von Josef auf `true` gesetzt) — jederzeit reversibel ohne Code-Änderung.
- PWA-Version 1.30 → 1.31.
