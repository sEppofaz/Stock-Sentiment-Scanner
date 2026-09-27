"""Langzeit-Tracker für Layer-7-Value-Signale: füllt value_forward_returns,
sobald 60/120/250 Handelstage seit dem Signal erreicht sind.

Bewusst eigene Horizonte statt der bestehenden 1/5/20 aus forward_returns/
scan_forward_returns: eine fundamentale Unterbewertung baut sich über Monate
ab, nicht über Tage – mit 20 Handelstagen würde man beim Value-Layer fast
ausschließlich Marktrauschen messen. 60/120/250 Handelstage ≈ 3/6/12 Monate.

Aufbau ansonsten strikt nach dem Muster von forward_tracker.py/scan_tracker.py:
- Referenzkurs IMMER frisch aus derselben split-bereinigten yfinance-Reihe wie
  der Horizont-Kurs (closes.iloc[0]), nie aus einer separat gespeicherten
  Quote – sonst liegen Zähler und Nenner auf unterschiedlichen Split-Skalen
  (ADR-019).
- Benchmark IWM/SPY im selben Fenster, pro (Symbol, Startdatum) innerhalb
  eines Laufs gecacht (Fable-Review 2026-09-11).

Zwei Unterschiede zu den beiden bestehenden Trackern:
1. Die offenen Zeilen legt NICHT der Scan an, sondern `ensure_rows()` beim
   Tracker-Lauf (LEFT JOIN über signals). Damit greift der Tracker auch
   rückwirkend auf die Value-Signale, die vor seiner Einführung entstanden
   sind (2026-09-12/19/26), ohne Backfill-Skript.
2. Signale, die jünger als `_MIN_AGE_CAL_DAYS` sind, werden komplett
   übersprungen – vor ~84 Kalendertagen kann nicht einmal der kürzeste
   Horizont (60 Handelstage) reif sein, ein Download wäre garantiert
   vergeblich. Bei wöchentlich 50 neuen Signalen spart das den Großteil aller
   yfinance-Requests.
"""
import logging
from datetime import datetime, timezone

from signals_db import get_conn
from yf_helper import fetch_closes

log = logging.getLogger("scanner")

_HORIZONS = (60, 120, 250)          # Handelstage ≈ 3/6/12 Monate
_MIN_AGE_CAL_DAYS = 84              # 60 Handelstage ≈ 84 Kalendertage
# Obergrenze: nach 500 Kalendertagen kann auch der längste Horizont
# (250 Handelstage ≈ 350 Kalendertage) nicht mehr reifen. Ohne diese Grenze
# würde jeder delistete Ticker mit dauerhaft leerer Kursreihe woche für Woche
# erneut heruntergeladen (im Logiktest 2026-09-27 aufgefallen) – die Arbeit je
# Lauf wäre unbegrenzt wachsend statt konstant.
_MAX_AGE_CAL_DAYS = 500
_BENCHMARKS = ("IWM", "SPY")

# Dedup-Fenster für die Auswertung, in KALENDERtagen (die Horizonte sind
# Handelstage, ~1,4 Kalendertage je Handelstag). Mit dem rohen Horizontwert
# (wie in weekly_analysis für 20 Tage) blieben die Fenster überlappend, und
# genau das soll die Deduplizierung verhindern – beim Value-Layer wiegt das
# schwerer, weil derselbe Ticker Woche für Woche erneut als Kandidat auftaucht.
_DEDUPE_CAL_DAYS = {60: 84, 120: 168, 250: 350}


def ensure_rows() -> int:
    """Legt für jedes value-Signal die drei offenen Horizont-Zeilen an
    (ret_pct NULL). Idempotent über INSERT OR IGNORE + Primary Key."""
    with get_conn() as conn:
        missing = conn.execute(
            "SELECT s.id FROM signals s WHERE s.signal_type='value' AND NOT EXISTS ("
            "  SELECT 1 FROM value_forward_returns v WHERE v.signal_id = s.id)"
        ).fetchall()
        conn.executemany(
            "INSERT OR IGNORE INTO value_forward_returns (signal_id, horizon_days) "
            "VALUES (?, ?)",
            [(r["id"], h) for r in missing for h in _HORIZONS],
        )
    return len(missing)


def _get_benchmark_closes(symbol: str, start_date: str, cache: dict):
    key = (symbol, start_date)
    if key not in cache:
        cache[key] = fetch_closes(symbol, start_date)
    return cache[key]


def run_value_tracker(cfg: dict) -> None:
    created = ensure_rows()
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat(timespec="seconds")

    with get_conn() as conn:
        open_rows = conn.execute(
            "SELECT v.signal_id, v.horizon_days, s.ticker, s.signal_ts "
            "FROM value_forward_returns v JOIN signals s ON s.id = v.signal_id "
            "WHERE v.ret_pct IS NULL AND s.signal_type='value'").fetchall()

    # Nach (ticker, signal_id) gruppieren – ein yfinance-Call pro Signal statt
    # einer pro Horizont (G7-Muster aus forward_tracker.py)
    by_signal: dict[tuple, list] = {}
    skipped_young = skipped_old = 0
    for r in open_rows:
        age = (now - datetime.fromisoformat(r["signal_ts"])).days
        if age < _MIN_AGE_CAL_DAYS:
            skipped_young += 1
            continue
        if age > _MAX_AGE_CAL_DAYS:
            skipped_old += 1
            continue
        by_signal.setdefault((r["ticker"], r["signal_id"]), []).append(r)

    bench_cache: dict[tuple, object] = {}
    filled = 0
    for (ticker, signal_id), rows in by_signal.items():
        signal_ts = rows[0]["signal_ts"]
        closes = fetch_closes(ticker, signal_ts[:10])
        if closes is None:
            continue
        bench_closes = {sym: _get_benchmark_closes(sym, signal_ts[:10], bench_cache)
                        for sym in _BENCHMARKS}
        baseline = float(closes.iloc[0])
        for r in rows:
            # Zeile 0 = Signaltag; Horizont h = h Handelstage danach
            if len(closes) <= r["horizon_days"]:
                continue
            try:
                ret = (float(closes.iloc[r["horizon_days"]]) / baseline - 1) * 100
                bench_rets = {}
                for sym, bc in bench_closes.items():
                    if bc is not None and len(bc) > r["horizon_days"]:
                        bench_rets[sym] = round(
                            (float(bc.iloc[r["horizon_days"]]) / float(bc.iloc[0]) - 1) * 100, 2)
                    else:
                        bench_rets[sym] = None
                with get_conn() as conn:
                    conn.execute(
                        "UPDATE value_forward_returns SET ret_pct=?, filled_ts=?, "
                        "price_at_signal=?, benchmark_iwm_ret_pct=?, benchmark_spy_ret_pct=? "
                        "WHERE signal_id=? AND horizon_days=?",
                        (round(ret, 2), now_iso, round(baseline, 4),
                         bench_rets["IWM"], bench_rets["SPY"], signal_id, r["horizon_days"]))
                filled += 1
            except Exception as e:
                log.warning("Value-Tracker %s h=%d: %s", ticker, r["horizon_days"], e)
    log.info("Value-Tracker: %d Returns gefüllt, %d neue Zeilen angelegt, "
             "%d noch zu jung (<%d Tage), %d endgültig aufgegeben (>%d Tage)",
             filled, created * len(_HORIZONS), skipped_young, _MIN_AGE_CAL_DAYS,
             skipped_old, _MAX_AGE_CAL_DAYS)


def analyze_value_performance() -> dict:
    """Trefferquote/Median/Payoff/Expectancy je Horizont – dieselbe Methodik wie
    weekly_analysis (Median statt Mittelwert, Deduplizierung überlappender
    Fenster, Benchmark-relative Zweitzahl), nur eigenständig für den Value-
    Layer. Bewusst KEIN Zusammenrechnen mit Sentiment-/Frühsignal-Zahlen: der
    Layer soll seine eigene Evidenz über Monate aufbauen (ADR-022-Lehre)."""
    from weekly_analysis import _dedupe_overlapping, _median, _mean

    with get_conn() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT s.ticker, s.signal_ts, s.score, v.horizon_days, v.ret_pct, "
            "       v.benchmark_iwm_ret_pct, v.benchmark_spy_ret_pct "
            "FROM value_forward_returns v JOIN signals s ON s.id = v.signal_id "
            "WHERE s.signal_type='value'")]

    total_signals = len({(r["ticker"], r["signal_ts"]) for r in rows})
    out = {"total_signals": total_signals, "horizons": {}}
    for h in _HORIZONS:
        hrows = [r for r in rows if r["horizon_days"] == h and r["ret_pct"] is not None]
        hrows = _dedupe_overlapping(hrows, "ticker", "signal_ts",
                                    window_days=_DEDUPE_CAL_DAYS[h])
        rets = [r["ret_pct"] for r in hrows]
        entry = {"n": len(rets), "pending": sum(
            1 for r in rows if r["horizon_days"] == h and r["ret_pct"] is None)}
        if rets:
            wins = [x for x in rets if x > 0]
            losses = [x for x in rets if x <= 0]
            med_w = _median(wins) or 0.0
            med_l = _median(losses) or 0.0
            hit = len(wins) / len(rets)
            excess = [r["ret_pct"] - r["benchmark_iwm_ret_pct"] for r in hrows
                      if r["benchmark_iwm_ret_pct"] is not None]
            entry.update({
                "hit_rate_pct": round(hit * 100, 1),
                "median_ret_pct": _median(rets),
                "mean_ret_pct": _mean(rets),
                "median_winner_pct": med_w,
                "median_loser_pct": med_l,
                # Payoff-Ratio auf Median-Basis (nicht Mittelwert) – 2026-09-11
                # hatten einzelne Datenfehler die Mittelwert-Variante auf
                # absurde 11 getrieben.
                "payoff_ratio": round(abs(med_w / med_l), 2) if med_l else None,
                "expectancy_pct": round(hit * med_w + (1 - hit) * med_l, 2),
                "vs_iwm_hit_rate_pct": round(
                    sum(1 for x in excess if x > 0) / len(excess) * 100, 1) if excess else None,
                "median_excess_iwm_pct": _median(excess),
            })
        out["horizons"][str(h)] = entry
    return out
