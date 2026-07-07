"""Layer 4: Kombinations-Scoring + Telegram-Alert. Läuft 1x täglich nach den EOD-Layern."""
import html
import json
import logging
from datetime import datetime, timezone

from signals_db import get_conn

log = logging.getLogger("scanner")


def run_scoring(cfg: dict) -> None:
    from scanner import _tg_post, _fetch_quote
    es = cfg.get("early_signals", {})
    min_score = es.get("alert_min_score", 4)
    min_types = es.get("alert_min_types", 2)
    cooldown = es.get("alert_cooldown_days", 7)
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")

    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, ticker, signal_type, signal_ts, score, details_json FROM signals "
            "WHERE signal_ts >= strftime('%Y-%m-%dT%H:%M:%S', 'now', '-7 days')").fetchall()
    by_ticker: dict[str, list] = {}
    for r in rows:
        by_ticker.setdefault(r["ticker"], []).append(r)

    alert_count = 0
    for ticker, sigs in by_ticker.items():
        types = {s["signal_type"] for s in sigs}
        if len(types) < min_types:
            continue
        # Gewichte: insider 3 (+2 Cluster), volume 2 (z>=4: 3), buzz 1 — pro Typ nur das stärkste Signal
        best: dict[str, float] = {}
        for s in sigs:
            d = json.loads(s["details_json"] or "{}")
            if s["signal_type"] == "insider_buy":
                w = 3.0 + (2.0 if d.get("cluster") else 0.0)
            elif s["signal_type"] == "volume_anomaly":
                w = 3.0 if d.get("z_score", 0) >= 4.0 else 2.0
            else:
                w = 1.0
            best[s["signal_type"]] = max(best.get(s["signal_type"], 0), w)
        total = sum(best.values())
        if total < min_score:
            continue

        with get_conn() as conn:
            recent_alert = conn.execute(
                "SELECT 1 FROM alerts WHERE ticker=? AND alert_ts >= strftime('%Y-%m-%dT%H:%M:%S', 'now', ?)",
                (ticker, f"-{cooldown} days")).fetchone()
        if recent_alert:
            continue

        price = _fetch_quote(ticker)
        sig_ids = [s["id"] for s in sigs]
        with get_conn() as conn:
            cur = conn.execute(
                "INSERT INTO alerts (ticker, alert_ts, total_score, signal_ids, price_at_alert) "
                "VALUES (?, ?, ?, ?, ?)",
                (ticker, now_iso, total, json.dumps(sig_ids), price))
            alert_id = cur.lastrowid
            if price is not None:
                # Ohne price_at_alert kann forward_tracker nie eine Rendite
                # berechnen (Division durch fehlende Baseline) – ohne Preis
                # keine Zeilen anlegen, sonst bleiben sie für immer tot (G7)
                conn.executemany(
                    "INSERT OR IGNORE INTO forward_returns (alert_id, horizon_days) VALUES (?, ?)",
                    [(alert_id, h) for h in (1, 5, 20)])

        lines = [f"🔮 <b>Frühsignal: {html.escape(ticker)}</b> — Score {total:.0f}"]
        for s in sorted(sigs, key=lambda x: x["signal_ts"]):
            d = json.loads(s["details_json"] or "{}")
            if s["signal_type"] == "insider_buy":
                extra = html.escape(d.get("filing_url", ""))
            elif s["signal_type"] == "volume_anomaly":
                extra = f"z={d.get('z_score')}"
            else:
                extra = f"accel={d.get('rel_accel')}"
            lines.append(f"• {s['signal_type']} {s['signal_ts'][:16]} {extra}")
        lines.append(f"Kurs: {price if price else '–'} USD | kein Anlagerat, Validierung läuft")
        _tg_post("\n".join(lines))
        log.info("Frühsignal-Alert: %s score=%.0f", ticker, total)
        alert_count += 1

    log.info("Scoring fertig: %d Alerts", alert_count)
