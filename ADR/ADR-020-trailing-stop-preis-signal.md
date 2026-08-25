# ADR-020: Preisbasiertes Verkaufssignal (Trailing-Stop) mit pro Position festgelegtem Prozentsatz

**Datum:** 2026-08-25
**Status:** aktiv
**Projekt:** Stock Sentiment Scanner

## Problem

Josef meldete (Todo #283), dass IMNM trotz eines Kursrücksetzers keine Verkaufsempfehlung bekam. Analyse ergab: Die beiden bestehenden Verkaufssignal-Quellen (`scanner._check_sell_signal()` – Sentiment-Umschwung, ADR-Vorgänger; `layer6_sell_signal.py` – Insider-Verkauf/Volumen-Anomalie, ADR-012) brauchen beide echte Marktdaten (News-Sentiment bzw. Insider-/Volumen-Signale). Bei einem Ticker ohne jede Presseabdeckung (IMNM: `bullish_pct`/`bearish_pct`/`buzz` durchgehend `0.0`) können beide Mechanismen strukturell nie ein Signal liefern, unabhängig vom tatsächlichen Kursverlauf. Es gab keinerlei reine Preislogik (kein Trailing-Stop, kein Take-Profit, kein Stop-Loss).

## Entscheidung

Neues drittes Verkaufssignal, rein auf Kursbasis: `peak_price` (Hoch seit Kauf, bei jedem Portfolio-Scan auf `max(peak_price, current_price)` aktualisiert) wird gegen einen Trailing-Stop-Prozentsatz geprüft. Fällt der Kurs um mindestens diesen Prozentsatz vom Hoch, wird dieselbe `sell_signal`/`sell_reason`-Infrastruktur gesetzt wie bei den beiden bestehenden Quellen (Wiederverwendungs-Muster aus ADR-012 fortgeführt), neue dritte `sell_signal_source`: `"preis"`.

Der Trailing-Stop-Prozentsatz (`trailing_stop_pct`) wird **pro Position** beim Kauf (Formular „Aktie hinzufügen") bzw. bei der Umwandlung Watch→Real abgefragt und in `portfolio.json` auf dem jeweiligen Eintrag gespeichert – NICHT als einzelner globaler Wert in `config.json`. Nachträglich änderbar per Inline-Edit direkt auf der Portfolio-Karte (PATCH `trailing_stop_pct`). `config.json` behält nur einen globalen Kill-Switch (`trailing_stop.enabled`).

Läuft unabhängig vom Sentiment-Fetch im selben Scan-Durchlauf (vorher blockierte ein fehlgeschlagener `_fetch_sentiment()`-Call auch die Preis-Prüfung, da beide hinter demselben `if sent is None: continue` lagen – als Nebeneffekt behoben).

## Begründung

- Pro-Position-Wert statt globaler Schwelle: Risikotoleranz/Überzeugung unterscheidet sich je nach Position (Josef-Wunsch, explizit gegen einen globalen Prozentwert entschieden).
- Trailing (vom Hoch seit Kauf) statt fixem Abstand vom Einstandskurs: schützt sowohl bereits gelaufene Gewinne (Take-Profit-Charakter) als auch gegen einen Verlust ohne vorherigen Anstieg (Stop-Loss-Charakter aus demselben Mechanismus, da `peak_price` beim ersten Scan mindestens auf `buy_price` startet).
- Wiederverwendung der bestehenden `sell_signal`-Infrastruktur (Banner, `resetSignal()`, Telegram-Alert) statt eigener Anzeige – analog ADR-012.
- Kein Auto-Reset bei Kurserholung (wie beim Frühsignal-Pfad, nicht wie beim Sentiment-Pfad) – ein ausgelöster Trailing-Stop ist ein einmaliger Hinweis, kein Zustand, der sich durch bloße Erholung wieder "auflöst".

## Verworfen

| Alternative | Warum verworfen |
|---|---|
| Globaler `drawdown_pct` in `config.json` (erster Entwurf) | Josef wollte den Prozentsatz bewusst pro Kaufentscheidung festlegen, nicht einheitlich für alle Positionen |
| Feste Schwelle ab Einstandskurs statt Trailing vom Hoch | Hätte bereits gelaufene Gewinne nicht geschützt – ein Ticker, der von $10 auf $15 lief und auf $13 zurückfällt (immer noch +30%), hätte kein Signal ausgelöst, obwohl ein erheblicher Teil des Gewinns bereits wieder abgegeben wurde |
| Retroaktiver globaler Default-Wert für Bestandspositionen ohne `trailing_stop_pct` | Hätte stillschweigend einen nicht von Josef gewählten Wert auf bestehende Positionen (z.B. IMNM) angewendet – stattdessen: kein Signal ohne explizit gesetzten Wert, nachträgliches Setzen per Inline-Edit |

## Gilt unter

Setzt voraus, dass zu jedem Zeitpunkt höchstens ein aktives Verkaufssignal pro Position existiert (wie ADR-012) – der neue Pfad prüft ebenfalls nur, wenn `sell_signal` noch nicht gesetzt ist, und überschreibt kein bestehendes Signal.

## Konsequenzen

- Positionen ohne gesetztes `trailing_stop_pct` (alle vor 2026-08-25 angelegten, aktuell nur IMNM als echte Position) bleiben ohne Preis-Signal, bis der Wert nachträglich über die Portfolio-Karte gesetzt wird.
- `run_portfolio_scan()` bekommt neu `cfg` als Parameter (vorher intern nicht verfügbar) – bei Aufrufen ohne mitgegebene Config (Hintergrund-Threads aus den Add/Convert-Endpoints) wird sie lazy nachgeladen.
- `_send_telegram_sell()` toleriert jetzt `sent=None` (Preis-Signal kann auch dann feuern, wenn der parallele Sentiment-Fetch im selben Zyklus fehlschlug).
