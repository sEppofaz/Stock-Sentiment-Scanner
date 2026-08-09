"""Layer 6: Tages-Konsolidierung – höchstens EIN Kauf-Pick pro Handelstag über
beide Systeme (Frühsignale + Sentiment-Scan) hinweg. Siehe Plan
atomic-soaring-codd.md (Fable-Gegenprüfung 2026-08-09)."""
import html
import json
import logging
from datetime import datetime, timezone

from signals_db import get_conn
from layer4_scoring import _et_day_start_utc_iso

log = logging.getLogger("scanner")

# Eigene, kleinere Konstante für Tagesfrische (Fable-Fix #4) – NICHT
# weekly_analysis._CROSS_OVERLAP_DAYS wiederverwenden, jener Wert ist für
# retrospektive Backtest-Statistik kalibriert, nicht für Tages-Bestätigung.
_DAILY_PICK_CROSS_DAYS = 2

_SOURCE_PRIORITY = {"cross_signal": 0, "early_signals": 1, "sentiment_scan": 2}


def _has_recent_insider_sell(ticker: str, min_usd: float) -> dict | None:
    """Neuestes insider_sell-Signal >= min_usd der letzten 7 Tage, sonst None."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT details_json FROM signals WHERE ticker=? AND signal_type='insider_sell' "
            "AND signal_ts >= strftime('%Y-%m-%dT%H:%M:%S', 'now', '-7 days') "
            "ORDER BY signal_ts DESC LIMIT 1", (ticker,)).fetchone()
    if not row:
        return None
    d = json.loads(row["details_json"] or "{}")
    if (d.get("total_usd") or 0) < min_usd:
        return None
    return d


def _has_cross_signal(ticker: str) -> bool:
    """Ticker hat sowohl einen Alert als auch einen Scan-Snapshot innerhalb
    der letzten _DAILY_PICK_CROSS_DAYS Tage (Fable-Fix #4)."""
    with get_conn() as conn:
        has_alert = conn.execute(
            "SELECT 1 FROM alerts WHERE ticker=? AND julianday('now') - julianday(alert_ts) <= ?",
            (ticker, _DAILY_PICK_CROSS_DAYS)).fetchone() is not None
        has_snapshot = conn.execute(
            "SELECT 1 FROM scan_snapshots WHERE ticker=? AND julianday('now') - julianday(snapshot_ts) <= ?",
            (ticker, _DAILY_PICK_CROSS_DAYS)).fetchone() is not None
    return has_alert and has_snapshot


def _distinct_signal_types_recent(ticker: str, days: int = 7) -> int:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(DISTINCT signal_type) c FROM signals WHERE ticker=? "
            "AND signal_ts >= strftime('%Y-%m-%dT%H:%M:%S', 'now', ?)",
            (ticker, f"-{days} days")).fetchone()
    return row["c"]


def _repicked_recently(ticker: str, cooldown_days: int) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM daily_picks WHERE ticker=? AND pick_date >= date('now', ?)",
            (ticker, f"-{cooldown_days} days")).fetchone()
    return row is not None


def _signals_for_ticker(ticker: str, days: int = 7) -> list[dict]:
    """Alle Frühsignale eines Tickers der letzten N Tage, für Veto-Check und
    Reasoning-Aufbereitung."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT signal_type, details_json FROM signals WHERE ticker=? "
            "AND signal_ts >= strftime('%Y-%m-%dT%H:%M:%S', 'now', ?) ORDER BY signal_ts DESC",
            (ticker, f"-{days} days")).fetchall()
    return [{"type": r["signal_type"], "raw": json.loads(r["details_json"] or "{}")} for r in rows]


def _format_signal_detail(sig: dict) -> str:
    t, d = sig["type"], sig["raw"]
    if t == "insider_buy":
        cluster = " (Cluster: weitere Insider in 7 Tagen)" if d.get("cluster") else ""
        return f"Insider-Kauf ${d.get('total_usd', 0):,.0f} von {d.get('owner') or '?'}{cluster}"
    if t == "insider_sell":
        cluster = " (Cluster)" if d.get("cluster") else ""
        return f"Insider-Verkauf ${d.get('total_usd', 0):,.0f} von {d.get('owner') or '?'}{cluster}"
    if t == "volume_anomaly":
        return f"Volumen-z-Score {d.get('z_score', '?')}"
    if t == "buzz_accel":
        return f"Buzz-Beschleunigung {d.get('rel_accel', '?')}x ggü. Vorwoche"
    if t == "large_holder":
        return f"{d.get('form_type', '?')}-Meldung {d.get('pct', '?')}% – {d.get('owner') or ''}"
    return t


def _check_liquidity(ticker: str, snap: dict | None,
                      min_avg_volume_10d: float, min_float_shares: float) -> bool:
    """avg_volume_10d/float_shares primär aus dem Scan-Snapshot (falls
    vorhanden); ohne Snapshot-Daten ein einzelner Finnhub-Call für den
    verbliebenen Finalisten (kein Bulk-Call)."""
    avg_vol = snap.get("avg_volume_10d") if snap else None
    float_sh = snap.get("float_shares") if snap else None
    if avg_vol is None or float_sh is None:
        try:
            from scanner import _fh_get
            if avg_vol is None:
                m = _fh_get("/stock/metric", {"symbol": ticker, "metric": "all"})
                avg_vol = (m.get("metric") or {}).get("10DayAverageTradingVolume")
            if float_sh is None:
                p = _fh_get("/stock/profile2", {"symbol": ticker})
                float_sh = p.get("floatingShare")
        except Exception as e:
            log.warning("Daily-Pick %s Liquiditäts-Check: %s", ticker, e)
    return (avg_vol or 0) >= min_avg_volume_10d and (float_sh or 0) >= min_float_shares


def _build_reasoning(winner: dict) -> dict:
    signals = winner.get("signals", [])
    reasoning = {
        "confirmations": winner.get("confirmations", []),
        "signals": [{"type": s["type"], "detail": _format_signal_detail(s)} for s in signals],
        "vetoes_checked": [
            "kein insider_sell (7 Tage) über Mindestbetrag",
            "z-Score unter Plausibilitätsgrenze",
            "kein Snapshot-Bearish-Übergewicht",
            "Liquidität ausreichend",
            "kein Repick innerhalb Cooldown",
        ],
    }
    snap = winner.get("snapshot")
    if snap:
        reasoning["sentiment_scan"] = {
            "rank": snap.get("rank"), "score": snap.get("score"),
            "bullish_pct": snap.get("bullish_pct"), "bearish_pct": snap.get("bearish_pct"),
            "buzz": snap.get("buzz"),
        }
    return reasoning


def _send_pick_telegram(winner: dict, reasoning: dict) -> None:
    from scanner import _tg_post
    ticker = winner["ticker"]
    top = reasoning["signals"][:2]
    detail_txt = " · ".join(s["detail"] for s in top) if top else ""
    lines = [f"🎯 <b>Tages-Pick: {html.escape(ticker)}</b>"]
    if detail_txt:
        lines.append(html.escape(detail_txt))
    ss = reasoning.get("sentiment_scan")
    if ss:
        lines.append(f"Sentiment-Scan: Bullish {ss.get('bullish_pct')}% · Score {ss.get('score')}")
    lines.append("Kein Anlagerat – Details/Rohwerte in der App, Validierung läuft.")
    _tg_post("\n".join(lines))


def _store_pick(pick_date: str, winner: dict) -> dict:
    reasoning = _build_reasoning(winner)
    alert = winner.get("alert")
    snap = winner.get("snapshot")
    price = (alert.get("price_at_alert") if alert else None) or \
            (snap.get("price_at_snapshot") if snap else None)
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")

    with get_conn() as conn:
        conn.execute(
            "INSERT INTO daily_picks (pick_date, ticker, source, source_alert_id, "
            "source_snapshot_id, reasoning_json, price_at_pick, created_ts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (pick_date, winner["ticker"], winner["source"],
             alert["id"] if alert else None, snap["id"] if snap else None,
             json.dumps(reasoning, ensure_ascii=False), price, now_iso))

    _send_pick_telegram(winner, reasoning)
    log.info("Daily-Pick %s: %s (Quelle %s)", pick_date, winner["ticker"], winner["source"])
    return {"pick_date": pick_date, "ticker": winner["ticker"], "source": winner["source"],
            "reasoning": reasoning, "price_at_pick": price}


def _store_no_pick(pick_date: str, n_candidates: int) -> dict:
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    reasoning = {"kandidaten_geprüft": n_candidates,
                 "grund": "kein Kandidat hat Bestätigung/Veto/Mindest-Score bestanden"}
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO daily_picks (pick_date, ticker, source, reasoning_json, created_ts) "
            "VALUES (?, NULL, NULL, ?, ?)",
            (pick_date, json.dumps(reasoning, ensure_ascii=False), now_iso))

    from scanner import _tg_post
    _tg_post(f"Kein Tages-Pick heute ({n_candidates} Kandidaten geprüft, keiner erfüllt die Kriterien).")
    log.info("Daily-Pick %s: kein Pick (%d Kandidaten geprüft)", pick_date, n_candidates)
    return {"pick_date": pick_date, "ticker": None, "reasoning": reasoning}


def _decide_and_store(cfg: dict, pick_date: str, force: bool = False) -> dict:
    dp = cfg.get("daily_pick", {})
    es = cfg.get("early_signals", {})
    min_usd = es.get("insider_min_usd", 25000)
    max_volume_z = dp.get("max_volume_z", 30)
    min_avg_volume_10d = dp.get("min_avg_volume_10d", 0)
    min_float_shares = dp.get("min_float_shares", 0)
    repick_cooldown_days = dp.get("repick_cooldown_days", 5)
    min_score_early = dp.get("min_score_early_signals", 5)
    min_score_sentiment = dp.get("min_score_sentiment_scan", 60)

    if force:
        with get_conn() as conn:
            conn.execute("DELETE FROM daily_picks WHERE pick_date=?", (pick_date,))

    day_start = _et_day_start_utc_iso()
    with get_conn() as conn:
        alerts = [dict(r) for r in conn.execute(
            "SELECT * FROM alerts WHERE alert_ts >= ?", (day_start,)).fetchall()]
        snapshots = [dict(r) for r in conn.execute(
            "SELECT * FROM scan_snapshots WHERE snapshot_ts >= ?", (day_start,)).fetchall()]

    candidates: dict[str, dict] = {}

    # C1/C2 aus heutigen Alerts
    for a in alerts:
        ticker = a["ticker"]
        confirmed_c1 = a["kind"] == "combo" or _distinct_signal_types_recent(ticker) >= 2
        cross = _has_cross_signal(ticker)
        if not (confirmed_c1 or cross):
            continue
        cand = candidates.setdefault(ticker, {"ticker": ticker, "alert": None,
                                               "snapshot": None, "confirmations": []})
        if cand["alert"] is None or (a["total_score"] or 0) > (cand["alert"]["total_score"] or 0):
            cand["alert"] = a
        if cross:
            cand["source"] = "cross_signal"
            if "cross_signal (auch im Sentiment-Scan)" not in cand["confirmations"]:
                cand["confirmations"].append("cross_signal (auch im Sentiment-Scan)")
        elif "source" not in cand:
            cand["source"] = "early_signals"
        label = "combo_alert" if a["kind"] == "combo" else "instant_alert"
        cand["confirmations"].append(f"{label} (Score {a['total_score']})")

    # C2/C3 aus heutigen Snapshots
    for s in snapshots:
        ticker = s["ticker"]
        cross = _has_cross_signal(ticker)
        confirmed_c3 = (s.get("rank") or 999) <= 5 and (s.get("bullish_pct") or 0) >= 70
        if not (cross or confirmed_c3):
            continue
        cand = candidates.setdefault(ticker, {"ticker": ticker, "alert": None,
                                               "snapshot": None, "confirmations": []})
        if cand.get("snapshot") is None or (s.get("rank") or 999) < (cand["snapshot"].get("rank") or 999):
            cand["snapshot"] = s
        if cross:
            cand["source"] = "cross_signal"
            if "cross_signal (auch bei Frühsignalen)" not in cand["confirmations"]:
                cand["confirmations"].append("cross_signal (auch bei Frühsignalen)")
        elif "source" not in cand:
            cand["source"] = "sentiment_scan"
        if confirmed_c3:
            cand["confirmations"].append(f"Sentiment-Scan Rang {s.get('rank')}, Bullish {s.get('bullish_pct')}%")

    survivors = []
    for ticker, cand in candidates.items():
        if _repicked_recently(ticker, repick_cooldown_days):
            continue  # V5
        if _has_recent_insider_sell(ticker, min_usd):
            continue  # V2

        signals = _signals_for_ticker(ticker)
        if any(sig["type"] == "volume_anomaly" and (sig["raw"].get("z_score") or 0) > max_volume_z
               for sig in signals):
            continue  # V1

        snap = cand.get("snapshot")
        if snap and (snap.get("bearish_pct") or 0) > (snap.get("bullish_pct") or 0):
            continue  # V3

        if not _check_liquidity(ticker, snap, min_avg_volume_10d, min_float_shares):
            continue  # V4

        source = cand.get("source", "early_signals")
        if source == "sentiment_scan" and snap:
            score = snap.get("score") or 0
            if score < min_score_sentiment:
                continue
        else:
            score = (cand["alert"]["total_score"] if cand.get("alert") else 0) or 0
            if source != "cross_signal" and score < min_score_early:
                continue

        survivors.append({**cand, "source": source, "score": score, "signals": signals})

    if not survivors:
        return _store_no_pick(pick_date, len(candidates))

    survivors.sort(key=lambda c: (_SOURCE_PRIORITY.get(c["source"], 9), -c["score"]))
    return _store_pick(pick_date, survivors[0])


def run_daily_pick(cfg: dict) -> dict | None:
    """Scheduler-Einstiegspunkt: idempotent, überspringt wenn für heute schon
    entschieden wurde (UNIQUE(pick_date))."""
    dp = cfg.get("daily_pick", {})
    if not dp.get("enabled", True):
        return None
    today = datetime.now(timezone.utc).date().isoformat()
    with get_conn() as conn:
        existing = conn.execute("SELECT 1 FROM daily_picks WHERE pick_date=?", (today,)).fetchone()
    if existing:
        log.info("Daily-Pick: für %s bereits entschieden, überspringe", today)
        return None
    return _decide_and_store(cfg, today, force=False)


def run_daily_pick_manual(cfg: dict, force: bool = False) -> dict:
    """Für POST /api/daily-pick/run. force=true: löscht die heutige Zeile und
    läuft neu (sonst hätte der Endpoint nach dem ersten Lauf des Tages keine
    Wirkung, Fable-Fix #5)."""
    today = datetime.now(timezone.utc).date().isoformat()
    if not force:
        with get_conn() as conn:
            existing = conn.execute(
                "SELECT pick_date, ticker, source, reasoning_json, price_at_pick "
                "FROM daily_picks WHERE pick_date=?", (today,)).fetchone()
        if existing:
            d = dict(existing)
            d["reasoning"] = json.loads(d.pop("reasoning_json") or "{}")
            return d
    return _decide_and_store(cfg, today, force=force)
