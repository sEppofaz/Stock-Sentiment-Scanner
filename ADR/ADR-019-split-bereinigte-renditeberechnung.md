# ADR-019: Split-bereinigte (auto_adjust=True) Renditeberechnung statt Rohkurse

**Datum:** 2026-08-21
**Status:** aktiv
**Projekt:** Stock Sentiment Scanner

## Problem

Im Zuge einer Performance-Analyse (Josef-Wunsch: "wo können wir ansetzen, um bessere Ergebnisse zu erzielen") fielen mehrere Ticker mit extremen Renditen (+662 % bis +14.811 %) auf. Stichprobenprüfung bei WETO ergab: Kein echter Kursgewinn, sondern ein **Reverse Split 1:100 am 03.08.2026** – die gespeicherte Rendite verglich einen Kurs von *vor* dem Split mit einem von *danach*, ohne Anpassung. Von 13 geprüften "Extrem-Gewinnern" hatten **10 einen Split** im jeweiligen Messfenster. `yf_helper.fetch_closes()` nutzte `auto_adjust=False` (unbereinigte Rohkurse) – bei einem Split zwischen Referenz-Tag (Alert/Snapshot) und Horizont-Tag (1/5/20 Handelstage später) sind Zähler und Nenner der Rendite-Formel auf unterschiedlicher Skala.

## Entscheidung

- `yf_helper.fetch_closes()` nutzt jetzt `auto_adjust=True` – yfinance liefert damit eine über den gesamten Zeitraum konsistent Split-bereinigte Kursreihe.
- `forward_tracker.py`/`scan_tracker.py` berechnen die Rendite jetzt IMMER aus zwei Punkten derselben frisch abgerufenen, bereinigten Kursreihe (`closes.iloc[0]` als Referenz, `closes.iloc[horizon_days]` als Horizont-Wert) – nicht mehr aus einer separat gespeicherten, potenziell unbereinigten Quelle (Finnhub-Live-Quote `price_at_alert` bzw. ein älterer `price_at_snapshot`-Wert). `price_at_alert` bleibt als reine Anzeige-Info (echter Kurs zum Alert-Zeitpunkt) unverändert erhalten, wird aber nicht mehr für die Rendite verwendet.
- **Einmalige Rückwirkende Neuberechnung** (Josef-Wunsch, nicht nur künftig fixen): `backfill_split_adjusted_returns.py` – Batch-Download (analog `layer2_volume.py`-Muster) aller betroffenen ~740 Ticker, Neuberechnung aller bereits gefüllten `ret_pct`-Werte in `forward_returns`/`scan_forward_returns` gegen die bereinigten Kurse, mit Vorher/Nachher-Log für Abweichungen ≥20 Prozentpunkte.

## Begründung

- `auto_adjust=True` ist die von yfinance selbst vorgesehene Lösung für genau dieses Problem – kein Custom-Split-Handling nötig, kein zusätzlicher API-Call (`Ticker().splits`) pro Berechnung.
- Beide Enden der Rendite-Berechnung aus derselben bereinigten Reihe zu nehmen (statt eine bereinigte mit einer unbereinigten externen Quelle zu mischen) vermeidet einen sonst spiegelbildlichen neuen Bug: eine bereinigte Horizont-Kurs gegen eine unbereinigte Referenz zu teilen wäre genauso falsch wie vorher, nur mit umgekehrtem Vorzeichen.
- Reverse Splits sind bei Sub-$1-Aktien (die häufigste Kategorie unter den Frühsignal-Kandidaten, meist zur Delisting-Vermeidung) verbreitet genug, dass der Bug nicht nur die 13 geprüften Einzelfälle betraf, sondern vermutlich einen relevanten Teil aller extremen Ausreißer im gesamten Datensatz.
- Rückwirkende Korrektur (statt nur "ab jetzt sauber"): Josefs explizite Entscheidung – die bereits gespeicherten Werte fließen in `weekly_analysis.py` (Median/Trefferquote/Schwellenwert-Vorschläge) ein, falsche historische Werte hätten diese Auswertungen dauerhaft verzerrt.

## Verworfen

| Alternative | Warum verworfen |
|---|---|
| Nur künftige Berechnungen fixen, alte Werte unverändert lassen | Josef wollte explizit auch die Historie korrigiert haben – die Analyse-Funktion (Median/Trefferquote, Schwellenwert-Vorschläge) hätte sonst dauerhaft mit verzerrten Daten gerechnet |
| Split-Erkennung selbst bauen (`Ticker().splits` abfragen, Kurs manuell reskalieren) | Unnötig – yfinance bietet mit `auto_adjust=True` exakt diese Bereinigung bereits eingebaut, ohne zusätzlichen API-Call |
| Einzelne `yf.download()`-Calls pro betroffenem Ticker im Backfill | Bei ~740 verschiedenen Tickern zu langsam – stattdessen Batch-Download in Chunks (CHUNK=150), analog zum bestehenden `layer2_volume.py`-Muster |

## Gilt unter

Setzt voraus, dass yfinance für die betroffenen (oft sehr kleinen, teils illiquiden) Ticker weiterhin korrekte Split-Daten liefert. Bei fehlenden/falschen Split-Daten in yfinance selbst würde der Fehler weiterhin bestehen – das ist aber ein Datenquellen-Problem, kein Logikfehler mehr im eigenen Code.

## Konsequenzen

- Alle künftigen `ret_pct`-Berechnungen sind Split-sicher.
- Historische Werte in `forward_returns`/`scan_forward_returns` wurden einmalig neu berechnet (siehe Logbuch-Eintrag für die konkreten Vorher/Nachher-Zahlen).
- `weekly_analysis.py`s Median/Trefferquote/Schwellenwert-Vorschläge basieren jetzt auf korrigierten Daten – frühere automatisch übernommene Schwellenwert-Vorschläge (`applied: true` in `weekly_reports`) könnten auf den alten, teils verzerrten Daten beruht haben; nicht rückwirkend revidiert (separates Thema, ggf. bei Gelegenheit prüfen).
