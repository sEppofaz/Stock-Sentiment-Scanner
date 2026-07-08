"""Layer 4: Kombinations-Scoring (täglich) + Instant-Alerts für starke Einzelsignale
(alle 15 Min) + Telegram-Alert + Auto-Watch. Siehe EARLY_SIGNALS_UMSETZUNG.md."""
import html
import json
import logging
from datetime import datetime, timezone

from signals_db import get_conn

log = logging.getLogger("scanner")


def _detail_line(s) -> str:
    d = json.loads(s["details_json"] or "{}")
    if s["signal_type"] == "insider_buy":
        extra = html.escape(d.get("filing_url", ""))
    elif s["signal_type"] == "volume_anomaly":
        extra = f"z={d.get('z_score')}"
    else:
        extra = f"accel={d.get('rel_accel')}"
    return f"• {s['signal_type']} {s['signal_ts'][:16]} {extra}"


def _auto_watch(cfg: dict, ticker: str, price: float | None) -> None:
    """Fügt den Ticker automatisch als Beobachtung (1 Test-Aktie, kein echter
    Kauf) ins Portfolio ein, wenn er noch nicht drin ist – damit die
    Kursentwicklung ab dem Signal-Zeitpunkt mitverfolgt wird, ohne dass Josef
    manuell reagieren muss. Reversibel über early_signals.auto_watch."""
    if not cfg.get("early_signals", {}).get("auto_watch", True):
        return
    if price is None:
        return
    from scanner import _load_portfolio, _save_portfolio
    portfolio = _load_portfolio()
    if any(p["ticker"] == ticker for p in portfolio):
        return
    portfolio.append({
        "ticker": ticker, "name": "", "shares": 1, "buy_price": price,
        "buy_date": datetime.now(timezone.utc).date().isoformat(),
        "last_sentiment": None, "current_price": price,
        "current_value": round(price, 2), "pnl": 0.0, "pnl_pct": 0.0,
        "sell_signal": False, "sell_reason": None, "watch": True,
    })
    _save_portfolio(portfolio)
    log.info("Auto-Watch: %s als Beobachtung ins Portfolio aufgenommen (Kurs %s)", ticker, price)


def _create_alert(cfg: dict, ticker: str, sigs: list, total_score: float,
                   cooldown_days: int, tag: str = "") -> bool:
    """Cooldown-Check + Alert-Insert + forward_returns + Telegram + Auto-Watch.
    Gemeinsamer Pfad für run_scoring() (Kombi, täglich) und
    check_instant_alerts() (starkes Einzelsignal, alle 15 Min). True wenn
    tatsächlich ausgelöst (False bei aktivem Cooldown)."""
    from scanner import _tg_post, _fetch_quote
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")

    with get_conn() as conn:
        recent_alert = conn.execute(
            "SELECT 1 FROM alerts WHERE ticker=? AND alert_ts >= strftime('%Y-%m-%dT%H:%M:%S', 'now', ?)",
            (ticker, f"-{cooldown_days} days")).fetchone()
    if recent_alert:
        return False

    price = _fetch_quote(ticker)
    sig_ids = [s["id"] for s in sigs]
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO alerts (ticker, alert_ts, total_score, signal_ids, price_at_alert) "
            "VALUES (?, ?, ?, ?, ?)",
            (ticker, now_iso, total_score, json.dumps(sig_ids), price))
        alert_id = cur.lastrowid
        if price is not None:
            # Ohne price_at_alert kann forward_tracker nie eine Rendite
            # berechnen (Division durch fehlende Baseline) – ohne Preis
            # keine Zeilen anlegen, sonst bleiben sie für immer tot (G7)
            conn.executemany(
                "INSERT OR IGNORE INTO forward_returns (alert_id, horizon_days) VALUES (?, ?)",
                [(alert_id, h) for h in (1, 5, 20)])

    label = f" ({tag})" if tag else ""
    lines = [f"🔮 <b>Frühsignal: {html.escape(ticker)}</b>{label} — Score {total_score:.0f}"]
    for s in sorted(sigs, key=lambda x: x["signal_ts"]):
        lines.append(_detail_line(s))
    lines.append(f"Kurs: {price if price else '–'} USD | kein Anlagerat, Validierung läuft")
    _tg_post("\n".join(lines))
    log.info("Frühsignal-Alert: %s score=%.0f%s", ticker, total_score, label)

    _auto_watch(cfg, ticker, price)
    return True


def run_scoring(cfg: dict) -> None:
    """Täglicher Kombi-Lauf: Alert wenn ≥ alert_min_types verschiedene
    Signal-Typen innerhalb von 7 Tagen zusammenkommen und ein Mindest-Score
    erreicht ist."""
    es = cfg.get("early_signals", {})
    min_score = es.get("alert_min_score", 4)
    min_types = es.get("alert_min_types", 2)
    cooldown = es.get("alert_cooldown_days", 7)

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
        if _create_alert(cfg, ticker, sigs, total, cooldown):
            alert_count += 1

    log.info("Scoring fertig: %d Alerts", alert_count)


def check_instant_alerts(cfg: dict) -> None:
    """Läuft alle 15 Min (ähnlich EDGAR-Job): prüft NEUE Signale (letzte 20
    Min) auf Einzelsignal-Stärke, unabhängig vom täglichen Kombi-Lauf. Für
    Fälle, in denen ein einzelnes Signal schon so stark ist, dass man nicht
    auf ein zweites/den Tagesabschluss warten will (Josef-Feedback
    2026-07-08: 'ich will früh wissen, wann ich aktiv werden sollte')."""
    es = cfg.get("early_signals", {})
    ins_min = es.get("single_insider_min_usd", 100000)
    vol_min = es.get("single_volume_z_min", 6.0)
    buzz_min = es.get("single_buzz_accel_min", 3.0)
    cooldown = es.get("alert_cooldown_days", 7)

    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, ticker, signal_type, signal_ts, score, details_json FROM signals "
            "WHERE signal_ts >= strftime('%Y-%m-%dT%H:%M:%S', 'now', '-20 minutes')").fetchall()

    fired = 0
    for r in rows:
        d = json.loads(r["details_json"] or "{}")
        if r["signal_type"] == "insider_buy":
            strong = bool(d.get("cluster")) or d.get("total_usd", 0) >= ins_min
        elif r["signal_type"] == "volume_anomaly":
            strong = d.get("z_score", 0) >= vol_min
        elif r["signal_type"] == "buzz_accel":
            strong = d.get("rel_accel", 0) >= buzz_min
        else:
            strong = False
        if not strong:
            continue
        if _create_alert(cfg, r["ticker"], [r], r["score"], cooldown, tag="Einzelsignal, stark"):
            fired += 1

    if fired:
        log.info("Instant-Alerts: %d ausgelöst", fired)
