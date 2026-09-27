"""Backtest der Exit-Regel: bringt ein Trailing-Stop auf den aktionablen
Signalen (ADR-022: insider_buy/large_holder) mehr als Halten bis Horizont-Ende?

Hintergrund: Fable-Analyse 2026-09-11, Empfehlung (4) – "Exit-/Risiko-
management verschaerfen statt weiter an Einstiegssignalen drehen". Dieses
Skript ist die Messgrundlage dafuer. Kostenlos (nur yfinance, auto_adjust=True
wie yf_helper.py), keine Finnhub-/Claude-Calls, schreibt nichts in signals.db.

Aufruf auf dem Server (Laufzeit ca. 3-5 Min, ein Download je Ticker):

    /opt/sentiment-scanner/venv/bin/python3 ts_backtest.py

Methodik
--------
- Population A: alerts, an denen insider_buy/large_holder beteiligt war
  (= was das System tatsaechlich als Kaufanlass meldet).
  Population B: alle insider_buy-Signale (breiter, gleicher Mechanismus).
- Beide je Ticker auf nicht-ueberlappende 20-Handelstage-Fenster dedupliziert
  (Muster weekly_analysis._dedupe_overlapping) – sonst zaehlt derselbe Ticker
  bei laufender Signallage mehrfach als vermeintlich unabhaengige Wette.
- Einstieg = Close des Signaltags (gleiche Konvention wie forward_tracker).
  Peak aus den High-Kursen der abgeschlossenen Tage, Ausloesung ueber das Low
  des laufenden Tages, Ausstieg zum Stop-Kurs. Bewusst nicht zum Tages-Close:
  der Live-Check laeuft alle 15 Min und trifft damit intraday.
- SLIPS modelliert, dass ein Stop auf illiquiden Microcaps NICHT exakt zum
  Stop-Kurs ausgefuehrt wird. Ohne diese Spalte ist das Ergebnis unbrauchbar –
  die engen Stops loesen in >90% der Faelle aus, jeder Prozentpunkt Slippage
  trifft sie also fast immer.
- Die Stichprobe wird zusaetzlich nach Signaldatum halbiert. Diese Spalte
  entscheidet, ob ein Ergebnis eine Regel oder nur das Marktregime des
  Zeitraums beschreibt.

Ergebnis des ersten Laufs (2026-09-27, n=207 ausgereifte insider_buy-Faelle,
Zeitraum 2026-07-06 bis 2026-08-26): gepoolt sehen enge Stops klar besser aus
(TS 5% Expectancy +1,71% vs. Halten -0,14%), aber die Rangfolge KIPPT zwischen
den beiden Stichprobenhaelften komplett (1. Haelfte: Halten +1,54% am besten;
2. Haelfte: Halten -2,74% am schlechtesten, TS 5% +1,33% am besten) – und
TS 5% loest in 94% der Faelle aus, womit 2% Slippage die Ueberrendite auf
+0,18% zusammenschmelzen lassen. Fazit: Die Stichprobe (7 Wochen je Haelfte,
ein einziges Marktregime) traegt keine Parameterwahl. Skript quartalsweise
erneut laufen lassen, sobald n und Regime-Abdeckung gewachsen sind.
"""
import json
import sqlite3
import statistics
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import yfinance as yf

DB = str(Path(__file__).parent / "signals.db")
STOPS = [0.05, 0.08, 0.10, 0.12, 0.15, 0.20, 0.25, 0.30]
SLIPS = [0.0, 0.005, 0.01, 0.02]     # Abschlag auf den Stop-Ausstiegskurs
MAXH = 20                            # Handelstage max. Haltedauer (= Haupthorizont)
DEDUPE_DAYS = 20
OUT = "/tmp/ts_backtest_result.json"


def load_populations() -> dict:
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    sig_type = {r["id"]: r["signal_type"] for r in c.execute("SELECT id, signal_type FROM signals")}
    pop_a = []
    for r in c.execute("SELECT ticker, alert_ts, signal_ids FROM alerts ORDER BY alert_ts"):
        try:
            ids = json.loads(r["signal_ids"] or "[]")
        except Exception:
            ids = []
        if {sig_type.get(i) for i in ids} & {"insider_buy", "large_holder"}:
            pop_a.append({"ticker": r["ticker"], "ts": r["alert_ts"]})
    pop_b = [{"ticker": r["ticker"], "ts": r["signal_ts"]} for r in c.execute(
        "SELECT ticker, signal_ts FROM signals WHERE signal_type=? ORDER BY signal_ts",
        ("insider_buy",))]
    c.close()
    return {"A_aktionable_alerts": dedupe(pop_a), "B_alle_insider_buy": dedupe(pop_b)}


def dedupe(rows: list) -> list:
    by_t: dict = {}
    for r in rows:
        by_t.setdefault(r["ticker"], []).append(r)
    kept = []
    for trows in by_t.values():
        trows.sort(key=lambda r: r["ts"])
        last = None
        for r in trows:
            ts = datetime.fromisoformat(r["ts"])
            if last is None or (ts - last).days >= DEDUPE_DAYS:
                kept.append(r)
                last = ts
    kept.sort(key=lambda r: r["ts"])
    return kept


def fetch_ohlc(tickers: set, start: str) -> dict:
    """Ein Download je Ticker ueber den gesamten Zeitraum – deutlich weniger
    Requests als das Tracker-Muster (dort einer je Ticker UND Datum)."""
    out = {}
    for i, t in enumerate(sorted(tickers), 1):
        try:
            h = yf.download(t, start=start, interval="1d", progress=False,
                            auto_adjust=True, threads=False)
            if h is None or h.empty:
                continue
            if isinstance(h.columns, pd.MultiIndex):
                h = h.xs(t, axis=1, level=1)
            h = h[["High", "Low", "Close"]].dropna()
            if not h.empty:
                out[t] = h
        except Exception as e:
            print("WARN %s: %s" % (t, e), file=sys.stderr)
        if i % 50 == 0:
            print("  ... %d/%d Ticker" % (i, len(tickers)), flush=True)
    return out


def simulate(bars, stop_pct, slip: float = 0.0):
    """Rueckgabe (ret_pct, gehaltene_handelstage, gestoppt) oder None."""
    entry = float(bars["Close"].iloc[0])
    if entry <= 0:
        return None
    peak = max(entry, float(bars["High"].iloc[0]))
    n = min(MAXH, len(bars) - 1)
    for i in range(1, n + 1):
        if stop_pct is not None:
            level = peak * (1 - stop_pct)
            if float(bars["Low"].iloc[i]) <= level:
                return ((level * (1 - slip) / entry - 1) * 100, i, True)
        peak = max(peak, float(bars["High"].iloc[i]))
    return ((float(bars["Close"].iloc[n]) / entry - 1) * 100, n, False)


def stats(rets: list) -> dict:
    n = len(rets)
    if not n:
        return {"n": 0}
    wins = [r for r in rets if r > 0]
    losses = [r for r in rets if r <= 0]
    med_w = statistics.median(wins) if wins else 0.0
    med_l = statistics.median(losses) if losses else 0.0
    hit = len(wins) / n
    return {"n": n, "hit_rate_pct": round(hit * 100, 1),
            "median_ret_pct": round(statistics.median(rets), 2),
            "mean_ret_pct": round(statistics.mean(rets), 2),
            "median_winner_pct": round(med_w, 2), "median_loser_pct": round(med_l, 2),
            "payoff_ratio": round(abs(med_w / med_l), 2) if med_l else None,
            "expectancy_pct": round(hit * med_w + (1 - hit) * med_l, 2)}


def variant_keys():
    return [("halten", None)] + [("TS %d%%" % int(s * 100), s) for s in STOPS]


def main():
    pops = load_populations()
    tickers = {r["ticker"] for rows in pops.values() for r in rows}
    first = min(r["ts"] for rows in pops.values() for r in rows)[:10]
    start = (datetime.fromisoformat(first) - timedelta(days=5)).date().isoformat()
    print("Ticker: %d, Start: %s" % (len(tickers), start), flush=True)
    data = fetch_ohlc(tickers, start)
    print("Kursdaten fuer %d/%d Ticker" % (len(data), len(tickers)), flush=True)

    result = {"generated": datetime.now().astimezone().isoformat(timespec="seconds"),
              "maxh_trading_days": MAXH, "dedupe_days": DEDUPE_DAYS, "populations": {}}

    for pname, rows in pops.items():
        cases = []
        for r in rows:
            bars = data.get(r["ticker"])
            if bars is None:
                continue
            sub = bars[bars.index >= r["ts"][:10]]
            if len(sub) < MAXH + 1:          # nur ausgereifte Faelle
                continue
            cases.append((r, sub))
        cases.sort(key=lambda c: c[0]["ts"])
        print("\n### %s (n roh %d, ausgereift %d)" % (pname, len(rows), len(cases)))
        if not cases:
            continue

        pres = {}
        print("%-9s %5s %7s %8s %9s %7s %8s" % (
            "Variante", "n", "Treffer", "Median", "Exp/Pos", "Payoff", "gestoppt"))
        for label, s in variant_keys():
            res = [x for x in (simulate(b, s) for _, b in cases) if x]
            st = stats([x[0] for x in res])
            st["stopped_pct"] = round(sum(1 for x in res if x[2]) / len(res) * 100, 1)
            st["avg_held_days"] = round(statistics.mean([x[1] for x in res]), 1)
            st["slippage"] = {}
            for slip in SLIPS:
                sres = [x for x in (simulate(b, s, slip) for _, b in cases) if x]
                st["slippage"]["%.3f" % slip] = stats([x[0] for x in sres])["expectancy_pct"]
            pres[label] = st
            print("%-9s %5d %6.1f%% %7.2f%% %8.2f%% %7s %7.0f%%" % (
                label, st["n"], st["hit_rate_pct"], st["median_ret_pct"],
                st["expectancy_pct"], st["payoff_ratio"], st["stopped_pct"]))

        print("  Expectancy je Slippage-Szenario (0 / 0,5 / 1 / 2 %):")
        for label, st in pres.items():
            print("   %-9s %s" % (label, "  ".join(
                "%+6.2f%%" % st["slippage"]["%.3f" % s] for s in SLIPS)))

        # Zeitstabilitaet: kippt die Rangfolge zwischen den Haelften, beschreibt
        # das Ergebnis das Marktregime und nicht die Regel.
        mid = len(cases) // 2
        halves = {"1. Haelfte": cases[:mid], "2. Haelfte": cases[mid:]}
        print("  Zeitstabilitaet (Slippage 1%):")
        pres_halves = {}
        for hlabel, sub in halves.items():
            if not sub:
                continue
            print("   %s %s..%s (n=%d)" % (hlabel, sub[0][0]["ts"][:10],
                                            sub[-1][0]["ts"][:10], len(sub)))
            pres_halves[hlabel] = {}
            for label, s in variant_keys():
                res = [x for x in (simulate(b, s, 0.01) for _, b in sub) if x]
                st = stats([x[0] for x in res])
                pres_halves[hlabel][label] = st
                print("     %-9s Treffer %5.1f%%  Median %+6.2f%%  Exp %+6.2f%%" % (
                    label, st["hit_rate_pct"], st["median_ret_pct"], st["expectancy_pct"]))

        result["populations"][pname] = {"n_signals_raw": len(rows), "n_matured": len(cases),
                                       "variants": pres, "halves": pres_halves}

    with open(OUT, "w") as f:
        json.dump(result, f, indent=1)
    print("\nfertig -> %s" % OUT)


if __name__ == "__main__":
    main()
