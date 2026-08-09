"""Layer 6b: Verkaufssignal aus Frühsignal-Gegensignalen (insider_sell,
volume_anomaly bei fallendem Kurs) – NUR für echte, bestätigte Positionen
(watch=False), nicht für Auto-Watch-Beobachtungen (Josef-Klarstellung, siehe
Plan atomic-soaring-codd.md). Setzt dieselben Felder wie
scanner._check_sell_signal() (Sentiment-Umschwung) weiter, damit die
bestehende PWA-Anzeige (🔴-Banner, resetSignal()) ohne neuen Code funktioniert."""
import html
import json
import logging

from signals_db import get_conn

log = logging.getLogger("scanner")

_LOOKBACK_MINUTES = 10


def _new_signals_for_tickers(tickers: list[str]) -> dict[str, list[dict]]:
    if not tickers:
        return {}
    placeholders = ",".join("?" * len(tickers))
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT ticker, signal_type, details_json FROM signals WHERE ticker IN ({placeholders}) "
            f"AND signal_ts >= strftime('%Y-%m-%dT%H:%M:%S', 'now', ?)",
            (*tickers, f"-{_LOOKBACK_MINUTES} minutes")).fetchall()
    out: dict[str, list[dict]] = {}
    for r in rows:
        out.setdefault(r["ticker"], []).append(
            {"type": r["signal_type"], "raw": json.loads(r["details_json"] or "{}")})
    return out


def check_frühsignal_sell_exits(cfg: dict) -> None:
    """Läuft alle 5 Min (versetztes Minuten-Raster, siehe app.py-Scheduler)
    während der Börsenzeit. Reine SQL-Abfrage gegen bereits von Layer 1-3
    gesammelte Daten, kein Bulk-Finnhub-Call – nur bei tatsächlichem
    Kandidaten ein einzelner _fetch_quote()-Call."""
    from scanner import _load_portfolio, _update_portfolio, _fetch_quote, _tg_post

    portfolio = _load_portfolio()
    real_positions = [p for p in portfolio if not p.get("watch") and not p.get("sell_signal")]
    if not real_positions:
        return

    tickers = [p["ticker"] for p in real_positions]
    signals_by_ticker = _new_signals_for_tickers(tickers)
    if not signals_by_ticker:
        return

    triggered: dict[str, str] = {}
    for p in real_positions:
        ticker = p["ticker"]
        sigs = signals_by_ticker.get(ticker, [])
        if not sigs:
            continue
        for sig in sigs:
            if sig["type"] == "insider_sell":
                d = sig["raw"]
                triggered[ticker] = f"Insider-Verkauf ${d.get('total_usd', 0):,.0f} von {d.get('owner') or '?'}"
                break
            if sig["type"] == "volume_anomaly":
                price = _fetch_quote(ticker)
                current = p.get("current_price")
                if price is not None and current and price < current:
                    triggered[ticker] = f"Volumen-Anomalie (z-Score {sig['raw'].get('z_score', '?')}) bei fallendem Kurs"
                    break

    if not triggered:
        return

    def _mutator(cur):
        for entry in cur:
            reason = triggered.get(entry["ticker"])
            if reason and not entry.get("sell_signal"):
                entry["sell_signal"] = True
                entry["sell_reason"] = f"Frühsignal: {reason}"
                entry["sell_signal_source"] = "fruehsignal"
        return cur

    _update_portfolio(_mutator)

    preview = ", ".join(f"{html.escape(t)} ({html.escape(r)})" for t, r in triggered.items())
    _tg_post(f"🔴 <b>Verkaufssignal(e)</b>: {preview} – siehe Portfolio-Tab")
    log.info("Frühsignal-Sell-Check: %d neue Verkaufssignale (%s)",
              len(triggered), ", ".join(triggered))
