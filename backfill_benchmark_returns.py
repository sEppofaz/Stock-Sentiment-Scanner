"""Einmal-Skript (2026-09-11): trägt IWM/SPY-Benchmark-Renditen für alle
bereits gefüllten forward_returns/scan_forward_returns-Zeilen rückwirkend
nach (Fable-Review 2026-09-11).

Hintergrund: Trefferquote/Median waren bisher absolut (ret_pct > 0), nicht
markt-relativ - bei einem fallenden Russell 2000/S&P 500 im Messzeitraum war
nicht unterscheidbar, ob eine negative Rendite am Signal selbst lag oder am
Gesamtmarkt. weekly_analysis.py zeigt ab diesem Deploy zusätzlich
overall_vs_iwm/overall_vs_spy (Excess-Return = ret_pct - Benchmark-Rendite
im selben Fenster) - dieses Skript befüllt die dafür nötigen Spalten für die
komplette bisherige Historie, nicht nur für künftige Zeilen.

Nutzung (einmalig, auf dem Server im venv):
    venv/bin/python3 backfill_benchmark_returns.py [--dry-run]
"""
import sys
import logging

import pandas as pd

from signals_db import get_conn
from yf_helper import fetch_closes

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("backfill_benchmark")

BENCHMARKS = ("IWM", "SPY")


def _load_targets():
    with get_conn() as conn:
        fwd = [dict(r) for r in conn.execute(
            "SELECT fr.alert_id AS id, fr.horizon_days, a.alert_ts AS ref_ts "
            "FROM forward_returns fr JOIN alerts a ON a.id = fr.alert_id "
            "WHERE fr.ret_pct IS NOT NULL"
        ).fetchall()]
        scan = [dict(r) for r in conn.execute(
            "SELECT sfr.snapshot_id AS id, sfr.horizon_days, s.snapshot_ts AS ref_ts "
            "FROM scan_forward_returns sfr JOIN scan_snapshots s ON s.id = sfr.snapshot_id "
            "WHERE sfr.ret_pct IS NOT NULL"
        ).fetchall()]
    return fwd, scan


def _download_benchmark(symbol: str, start: str):
    """Eine einzige Kursreihe ab dem frühesten benötigten Datum - deutlich
    günstiger als ein Call pro Zeile (analog backfill_split_adjusted_returns.py)."""
    closes = fetch_closes(symbol, start)
    if closes is None:
        return None
    if closes.index.tz is not None:
        closes.index = closes.index.tz_localize(None)
    return closes


def _compute(rows: list[dict], closes: pd.Series) -> dict[tuple, float]:
    """{(id, horizon_days): ret_pct} - Referenz-Tag wird per searchsorted in
    der einmal geladenen Gesamtreihe lokalisiert (die Reihe startet vor dem
    frühesten Referenz-Tag, nicht erst dort)."""
    out = {}
    for r in rows:
        ref_date = pd.Timestamp(r["ref_ts"][:10])
        pos = closes.index.searchsorted(ref_date)
        if pos >= len(closes) or pos + r["horizon_days"] >= len(closes):
            continue
        baseline = float(closes.iloc[pos])
        if baseline <= 0:
            continue
        value = float(closes.iloc[pos + r["horizon_days"]])
        out[(r["id"], r["horizon_days"])] = round((value / baseline - 1) * 100, 2)
    return out


def _apply(table: str, id_col: str, rows: list[dict], iwm: dict, spy: dict, dry_run: bool) -> int:
    n = 0
    with get_conn() as conn:
        for r in rows:
            key = (r["id"], r["horizon_days"])
            iwm_ret, spy_ret = iwm.get(key), spy.get(key)
            if iwm_ret is None and spy_ret is None:
                continue
            n += 1
            if not dry_run:
                conn.execute(
                    f"UPDATE {table} SET benchmark_iwm_ret_pct=?, benchmark_spy_ret_pct=? "
                    f"WHERE {id_col}=? AND horizon_days=?",
                    (iwm_ret, spy_ret, r["id"], r["horizon_days"]))
    return n


def main():
    dry_run = "--dry-run" in sys.argv

    fwd, scan = _load_targets()
    log.info("forward_returns: %d Zeilen, scan_forward_returns: %d Zeilen", len(fwd), len(scan))
    if not fwd and not scan:
        log.info("Nichts zu tun.")
        return

    earliest = min(r["ref_ts"][:10] for r in (fwd + scan))
    log.info("Benchmark-Historie ab %s", earliest)

    closes = {}
    for sym in BENCHMARKS:
        c = _download_benchmark(sym, earliest)
        if c is None:
            log.warning("Keine Kursdaten für %s - wird übersprungen", sym)
        else:
            log.info("%s: %d Handelstage geladen", sym, len(c))
        closes[sym] = c

    iwm_fwd = _compute(fwd, closes["IWM"]) if closes.get("IWM") is not None else {}
    spy_fwd = _compute(fwd, closes["SPY"]) if closes.get("SPY") is not None else {}
    iwm_scan = _compute(scan, closes["IWM"]) if closes.get("IWM") is not None else {}
    spy_scan = _compute(scan, closes["SPY"]) if closes.get("SPY") is not None else {}

    n_fwd = _apply("forward_returns", "alert_id", fwd, iwm_fwd, spy_fwd, dry_run)
    n_scan = _apply("scan_forward_returns", "snapshot_id", scan, iwm_scan, spy_scan, dry_run)

    log.info("forward_returns: %d/%d Zeilen mit Benchmark-Wert versehen", n_fwd, len(fwd))
    log.info("scan_forward_returns: %d/%d Zeilen mit Benchmark-Wert versehen", n_scan, len(scan))
    if dry_run:
        log.info("--dry-run: keine Änderungen geschrieben.")


if __name__ == "__main__":
    main()
