"""Einmal-Skript (2026-08-21): berechnet alle bereits gefüllten ret_pct-Werte
in forward_returns/scan_forward_returns neu, mit Split-bereinigten
(auto_adjust=True) yfinance-Kursen statt der bisherigen unbereinigten.

Hintergrund: yf_helper.fetch_closes() nutzte bisher auto_adjust=False. Lag
zwischen Referenz-Tag (Alert/Snapshot) und Horizont-Tag ein Aktien-Split
(v.a. Reverse Splits bei Sub-$1-Aktien, um Delisting zu vermeiden), waren
Zähler und Nenner der Rendite-Berechnung auf unterschiedlicher Skala -
Ergebnis: Renditen von mehreren tausend Prozent, die real nicht stattfanden
(live gefunden: WETO zeigte +7.322%, tatsächlich fand am 03.08.2026 ein
Reverse Split 1:100 statt). 10 von 13 geprüften "Extrem-Gewinnern" aus der
Performance-Analyse vom 21.08.2026 hatten einen Split im Messfenster.

Nutzung (einmalig, auf dem Server im venv):
    venv/bin/python3 backfill_split_adjusted_returns.py [--dry-run]

Batch-Download analog layer2_volume.py (CHUNK=200), damit die ~740
betroffenen Ticker nicht einzeln nacheinander abgefragt werden (mehrere
tausend Einzel-Calls hätten sehr lange gedauert)."""
import sys
import logging
from datetime import date

import pandas as pd
import yfinance as yf

from signals_db import get_conn

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("backfill")

CHUNK = 150


def _load_targets():
    with get_conn() as conn:
        fwd = [dict(r) for r in conn.execute(
            "SELECT fr.alert_id AS id, fr.horizon_days, fr.ret_pct AS old_ret_pct, "
            "       a.ticker, a.alert_ts AS ref_ts "
            "FROM forward_returns fr JOIN alerts a ON a.id = fr.alert_id "
            "WHERE fr.ret_pct IS NOT NULL"
        ).fetchall()]
        scan = [dict(r) for r in conn.execute(
            "SELECT sfr.snapshot_id AS id, sfr.horizon_days, sfr.ret_pct AS old_ret_pct, "
            "       s.ticker, s.snapshot_ts AS ref_ts "
            "FROM scan_forward_returns sfr JOIN scan_snapshots s ON s.id = sfr.snapshot_id "
            "WHERE sfr.ret_pct IS NOT NULL"
        ).fetchall()]
    return fwd, scan


def _download_all(tickers: list[str], start: str) -> dict:
    """Liefert {ticker: Close-Series (tz-naive DatetimeIndex, dropna)}."""
    closes_by_ticker = {}
    for i in range(0, len(tickers), CHUNK):
        chunk = tickers[i:i + CHUNK]
        log.info("Download Chunk %d/%d (%d Ticker)…", i // CHUNK + 1,
                 (len(tickers) + CHUNK - 1) // CHUNK, len(chunk))
        try:
            data = yf.download(tickers=chunk, start=start, interval="1d",
                                group_by="ticker", threads=True, progress=False,
                                auto_adjust=True)
        except Exception as e:
            log.warning("Chunk-Download fehlgeschlagen: %s", e)
            continue
        for sym in chunk:
            try:
                s = data[sym]["Close"].dropna()
            except (KeyError, TypeError):
                continue
            if s.empty:
                continue
            s.index = s.index.tz_localize(None)
            closes_by_ticker[sym] = s
    return closes_by_ticker


def _recompute(rows: list[dict], closes_by_ticker: dict, dry_run: bool) -> list[dict]:
    changes = []
    for r in rows:
        closes = closes_by_ticker.get(r["ticker"])
        if closes is None:
            continue
        ref_date = pd.Timestamp(r["ref_ts"][:10])
        pos = closes.index.searchsorted(ref_date)
        if pos >= len(closes) or pos + r["horizon_days"] >= len(closes):
            continue
        baseline = float(closes.iloc[pos])
        value = float(closes.iloc[pos + r["horizon_days"]])
        if baseline <= 0:
            continue
        new_ret = round((value / baseline - 1) * 100, 2)
        old_ret = r["old_ret_pct"]
        changes.append({**r, "new_ret_pct": new_ret, "delta": round(new_ret - old_ret, 2)})
    return changes


def _apply(table: str, id_col: str, changes: list[dict]) -> None:
    with get_conn() as conn:
        for c in changes:
            conn.execute(
                f"UPDATE {table} SET ret_pct=? WHERE {id_col}=? AND horizon_days=?",
                (c["new_ret_pct"], c["id"], c["horizon_days"]))


def main():
    dry_run = "--dry-run" in sys.argv

    fwd, scan = _load_targets()
    log.info("forward_returns: %d Zeilen, scan_forward_returns: %d Zeilen", len(fwd), len(scan))

    all_tickers = sorted({r["ticker"] for r in fwd} | {r["ticker"] for r in scan})
    earliest = min(r["ref_ts"][:10] for r in (fwd + scan))
    log.info("%d verschiedene Ticker, ab %s", len(all_tickers), earliest)

    closes_by_ticker = _download_all(all_tickers, earliest)
    log.info("Kursreihen für %d/%d Ticker erhalten", len(closes_by_ticker), len(all_tickers))

    fwd_changes = _recompute(fwd, closes_by_ticker, dry_run)
    scan_changes = _recompute(scan, closes_by_ticker, dry_run)

    def _summary(name, changes):
        big = [c for c in changes if abs(c["delta"]) >= 20]
        log.info("%s: %d neu berechnet, %d davon mit Abweichung >=20 Prozentpunkte",
                  name, len(changes), len(big))
        for c in sorted(big, key=lambda c: -abs(c["delta"]))[:15]:
            log.info("  %s h=%d: %.2f%% -> %.2f%% (Δ %.2f)",
                      c["ticker"], c["horizon_days"], c["old_ret_pct"], c["new_ret_pct"], c["delta"])

    _summary("forward_returns", fwd_changes)
    _summary("scan_forward_returns", scan_changes)

    if dry_run:
        log.info("--dry-run: keine Änderungen geschrieben.")
        return

    _apply("forward_returns", "alert_id", fwd_changes)
    _apply("scan_forward_returns", "snapshot_id", scan_changes)
    log.info("Fertig: %d + %d ret_pct-Werte aktualisiert.", len(fwd_changes), len(scan_changes))


if __name__ == "__main__":
    main()
