import os
import csv
import json
import logging
import logging.handlers
import re
import threading
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from flask import Flask, jsonify, request, send_file, Response
from apscheduler.schedulers.background import BackgroundScheduler

BASE_DIR = Path(__file__).parent
ICONS_DIR = BASE_DIR / "icons"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[
        # Rotierend statt unbegrenzt wachsend (M8): 5 MB x 3 Backups
        logging.handlers.RotatingFileHandler(
            BASE_DIR / "scan.log", maxBytes=5_000_000, backupCount=3
        ),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("app")

app = Flask(__name__)

# ── Icon ─────────────────────────────────────────────────────────────────────

_ICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="-6 -6 36 36">'
    '<rect x="-6" y="-6" width="36" height="36" fill="#065f46"/>'
    '<polyline points="22 7 13.5 15.5 8.5 10.5 2 17" stroke="white" '
    'stroke-width="1.8" fill="none" stroke-linecap="round" stroke-linejoin="round"/>'
    '<polyline points="16 7 22 7 22 13" stroke="white" '
    'stroke-width="1.8" fill="none" stroke-linecap="round" stroke-linejoin="round"/>'
    "</svg>"
)


def _make_icon(size: int, fname: str):
    import cairosvg
    ICONS_DIR.mkdir(parents=True, exist_ok=True)
    data = cairosvg.svg2png(
        bytestring=_ICON_SVG.encode(), output_width=size, output_height=size
    )
    (ICONS_DIR / fname).write_bytes(data)


def _serve_icon(size: int, fname: str):
    p = ICONS_DIR / fname
    if not p.exists():
        _make_icon(size, fname)
    return send_file(p, mimetype="image/png")


# ── Config ────────────────────────────────────────────────────────────────────

def _load_cfg() -> dict:
    path = BASE_DIR / "config.json"
    if not path.exists():
        import shutil
        shutil.copy(BASE_DIR / "config.default.json", path)
    return json.loads(path.read_text())


_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def _validate_cfg(cfg) -> str | None:
    """Gibt eine Fehlermeldung zurück wenn cfg ungültig ist, sonst None.

    Verhindert, dass eine kaputte Config geschrieben wird, die _reschedule()
    beim nächsten Service-Neustart crashen lässt (M7: Config-Validierung).
    """
    if not isinstance(cfg, dict):
        return "Config muss ein Objekt sein"

    times = cfg.get("scan_times_utc", [])
    if not isinstance(times, list) or not all(
        isinstance(t, str) and _TIME_RE.match(t) for t in times
    ):
        return "scan_times_utc muss eine Liste von 'HH:MM'-Strings sein"

    f = cfg.get("filter", {})
    if not isinstance(f, dict):
        return "filter muss ein Objekt sein"
    for key in ("bullish_pct_min", "bearish_pct_max", "news_min_count",
                "market_cap_min_usd", "market_cap_max_usd"):
        if key in f and not isinstance(f[key], (int, float)):
            return f"filter.{key} muss numerisch sein"

    if "top_n_results" in cfg and not isinstance(cfg["top_n_results"], (int, float)):
        return "top_n_results muss numerisch sein"

    return None


# ── Portfolio ─────────────────────────────────────────────────────────────────

def _load_portfolio() -> list[dict]:
    path = BASE_DIR / "portfolio.json"
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text())
    except Exception:
        return []


def _save_portfolio(data: list[dict]):
    path = BASE_DIR / "portfolio.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    tmp.rename(path)


# ── Scheduler ─────────────────────────────────────────────────────────────────

NYSE_TZ = ZoneInfo("America/New_York")


def _market_open() -> bool:
    """True wenn NYSE/NASDAQ geöffnet (Mo–Fr 9:30–16:00 America/New_York).

    DST-sicher über zoneinfo statt fixer UTC-Grenzen – die alte Version
    (14:30–21:00 UTC) war nur im Winter (EST) korrekt; im Sommer (EDT,
    z.B. Juli) fehlte dadurch die erste Handelsstunde (13:30–14:30 UTC).
    """
    now = datetime.now(NYSE_TZ)
    if now.weekday() >= 5:
        return False
    t = now.hour * 60 + now.minute
    return 570 <= t <= 960  # 9:30=570, 16:00=960


scheduler = BackgroundScheduler()


def _reschedule():
    cfg = _load_cfg()
    scheduler.remove_all_jobs()

    # Volle Scans (aus config, Zeiten sind UTC – Server-Systemzeit ist Europe/Berlin!)
    # Pro Eintrag try/except: eine manuell kaputt editierte config.json soll nicht
    # ALLE Jobs (inkl. Portfolio-Scan + Frühsignale) mit reißen (M7).
    for t in cfg.get("scan_times_utc", []):
        try:
            h, m = map(int, t.split(":"))
            scheduler.add_job(
                _do_full_scan, "cron",
                hour=h, minute=m, day_of_week="mon-fri",
                timezone="UTC", id=f"scan_{h:02d}{m:02d}",
            )
        except Exception:
            log.exception("Ungültige Scan-Zeit übersprungen: %r", t)

    # Portfolio-Scan: alle 15 Min Mo–Fr 9:00–16:45 America/New_York
    # (DST-sicher; _market_open()-Guard grenzt auf die echte Handelszeit 9:30–16:00 ein)
    scheduler.add_job(
        _do_portfolio_scan, "cron",
        hour="9-16", minute="0,15,30,45", day_of_week="mon-fri",
        timezone="America/New_York", id="portfolio_scan",
    )

    # Tägliches Cleanup alter buzz_history/edgar_seen-Zeilen (M8) – unabhängig
    # von early_signals.enabled, da buzz_history immer aus dem Vollscan befüllt wird
    scheduler.add_job(
        _do_cleanup, "cron", hour=3, minute=0, timezone="UTC", id="daily_cleanup",
    )

    # Frühsignale (EARLY_SIGNALS_UMSETZUNG.md)
    if cfg.get("early_signals", {}).get("enabled", False):
        scheduler.add_job(
            _do_edgar_scan, "cron",
            hour="6-22", minute="*/15", day_of_week="mon-fri",
            timezone="America/New_York", id="edgar_scan",
        )
        # Layer 5: SC 13D/13G Großaktionärs-Meldungen (2026-07-08, Josef-Feedback)
        scheduler.add_job(
            _do_ownership_scan, "cron",
            hour="6-22", minute="*/15", day_of_week="mon-fri",
            timezone="America/New_York", id="ownership_scan",
        )
        scheduler.add_job(
            _do_volume_scan, "cron", hour=17, minute=15,
            day_of_week="mon-fri", timezone="America/New_York", id="volume_scan",
        )
        scheduler.add_job(
            _do_buzz_accel, "cron", hour=17, minute=25,
            day_of_week="mon-fri", timezone="America/New_York", id="buzz_accel",
        )
        scheduler.add_job(
            _do_es_scoring, "cron", hour=17, minute=35,
            day_of_week="mon-fri", timezone="America/New_York", id="es_scoring",
        )
        scheduler.add_job(
            _do_fwd_tracker, "cron", hour=17, minute=45,
            day_of_week="mon-fri", timezone="America/New_York", id="es_tracker",
        )
        # Instant-Alerts für starke Einzelsignale (Josef-Feedback 2026-07-08:
        # nicht auf ein zweites Signal / den Tagesabschluss warten müssen)
        scheduler.add_job(
            _do_es_instant, "cron",
            hour="6-22", minute="*/15", day_of_week="mon-fri",
            timezone="America/New_York", id="es_instant",
        )

    log.info(
        "Scan-Zeiten: %s (Mo–Fr UTC) + Portfolio-Scan alle 15 Min 9:00–16:45 America/New_York",
        cfg.get("scan_times_utc"),
    )


def _do_full_scan():
    if not _load_cfg().get("scan_enabled", True):
        log.info("Vollständiger Scan übersprungen – Scan deaktiviert")
        return
    from scanner import run_scan, SCAN_STATUS
    if SCAN_STATUS.get("running"):
        log.info("Vollständiger Scan übersprungen – Scan läuft bereits")
        return
    try:
        run_scan(_load_cfg())
    except Exception:
        log.exception("Vollständiger Scan-Fehler")


def _do_portfolio_scan():
    if not _load_cfg().get("scan_enabled", True):
        return
    if not _market_open():
        return
    from scanner import run_portfolio_scan, SCAN_STATUS
    if SCAN_STATUS.get("running"):
        log.info("Portfolio-Scan übersprungen – voller Scan läuft noch")
        return
    try:
        run_portfolio_scan()
    except Exception:
        log.exception("Portfolio-Scan-Fehler")


def _do_edgar_scan():
    cfg = _load_cfg()
    if not cfg.get("early_signals", {}).get("enabled", False):
        return
    try:
        from layer1_edgar import run_edgar_scan
        run_edgar_scan(cfg)
    except Exception:
        log.exception("EDGAR-Scan fehlgeschlagen")


def _do_ownership_scan():
    cfg = _load_cfg()
    if not cfg.get("early_signals", {}).get("enabled", False):
        return
    try:
        from layer5_ownership import run_ownership_scan
        run_ownership_scan(cfg)
    except Exception:
        log.exception("Ownership-Scan (13D/13G) fehlgeschlagen")


def _do_volume_scan():
    cfg = _load_cfg()
    if not cfg.get("early_signals", {}).get("enabled", False):
        return
    try:
        from layer2_volume import run_volume_scan
        run_volume_scan(cfg)
    except Exception:
        log.exception("Volumen-Scan fehlgeschlagen")


def _do_buzz_accel():
    cfg = _load_cfg()
    if not cfg.get("early_signals", {}).get("enabled", False):
        return
    try:
        from layer3_buzz import run_buzz_accel
        run_buzz_accel(cfg)
    except Exception:
        log.exception("Buzz-Accel fehlgeschlagen")


def _do_es_scoring():
    cfg = _load_cfg()
    if not cfg.get("early_signals", {}).get("enabled", False):
        return
    try:
        from layer4_scoring import run_scoring
        run_scoring(cfg)
    except Exception:
        log.exception("Frühsignal-Scoring fehlgeschlagen")


def _do_fwd_tracker():
    cfg = _load_cfg()
    if not cfg.get("early_signals", {}).get("enabled", False):
        return
    try:
        from forward_tracker import run_tracker
        run_tracker(cfg)
    except Exception:
        log.exception("Forward-Tracker fehlgeschlagen")


def _do_es_instant():
    cfg = _load_cfg()
    if not cfg.get("early_signals", {}).get("enabled", False):
        return
    try:
        from layer4_scoring import check_instant_alerts
        check_instant_alerts(cfg)
    except Exception:
        log.exception("Instant-Alert-Check fehlgeschlagen")


def _do_cleanup():
    try:
        from signals_db import cleanup_old_data
        buzz_deleted, edgar_deleted = cleanup_old_data()
        log.info("Cleanup: %d buzz_history- + %d edgar_seen-Zeilen entfernt (>60/>30 Tage)",
                  buzz_deleted, edgar_deleted)
    except Exception:
        log.exception("Cleanup fehlgeschlagen")


from signals_db import init_db
init_db()

scheduler.start()
_reschedule()

# ── PWA-Dateien ───────────────────────────────────────────────────────────────

@app.route("/sentiment/")
def index():
    return send_file(BASE_DIR / "pwa" / "index.html")


@app.route("/sentiment/manifest.json")
def manifest():
    return send_file(
        BASE_DIR / "pwa" / "manifest.json",
        mimetype="application/manifest+json",
    )


@app.route("/sentiment/sw.js")
def sw():
    resp = send_file(BASE_DIR / "pwa" / "sw.js", mimetype="application/javascript")
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/sentiment/icon-192.png")
def icon192():
    return _serve_icon(192, "icon-192.png")


@app.route("/sentiment/icon-512.png")
def icon512():
    return _serve_icon(512, "icon-512.png")


@app.route("/sentiment/apple-touch-icon.png")
def apple_icon():
    return _serve_icon(180, "apple-touch-icon.png")


# ── API: Scan ─────────────────────────────────────────────────────────────────

@app.route("/sentiment/api/results")
def api_results():
    path = BASE_DIR / "results.json"
    if not path.exists():
        return jsonify({"error": "Noch kein Scan durchgeführt"}), 404
    return Response(path.read_text(), mimetype="application/json")


@app.route("/sentiment/api/scan", methods=["POST"])
def api_scan_trigger():
    if datetime.utcnow().weekday() >= 5:
        return jsonify({"ok": False, "message": "Kein Scan am Wochenende"}), 409
    from scanner import SCAN_STATUS
    if SCAN_STATUS.get("running"):
        return jsonify({"ok": False, "message": "Scan läuft bereits"}), 409
    threading.Thread(target=_do_full_scan, daemon=True).start()
    return jsonify({"ok": True, "message": "Scan gestartet"})


@app.route("/sentiment/api/portfolio/scan", methods=["POST"])
def api_portfolio_scan_trigger():
    from scanner import SCAN_STATUS
    if SCAN_STATUS.get("running"):
        return jsonify({"ok": False, "message": "Scan läuft bereits"}), 409
    threading.Thread(target=_do_portfolio_scan, daemon=True).start()
    return jsonify({"ok": True, "message": "Portfolio-Scan gestartet"})


@app.route("/sentiment/api/scan/status")
def api_scan_status():
    from scanner import SCAN_STATUS
    return jsonify(SCAN_STATUS)


@app.route("/sentiment/api/scan/abort", methods=["POST"])
def api_scan_abort():
    from scanner import SCAN_STATUS
    if SCAN_STATUS.get("running"):
        SCAN_STATUS["abort"] = True
        return jsonify({"ok": True, "message": "Abbruch angefordert"})
    return jsonify({"ok": False, "message": "Kein Scan läuft"})


# ── API: Config ───────────────────────────────────────────────────────────────

@app.route("/sentiment/api/config", methods=["GET"])
def api_config_get():
    return jsonify(_load_cfg())


@app.route("/sentiment/api/config", methods=["POST"])
def api_config_set():
    cfg = request.get_json(force=True)
    err = _validate_cfg(cfg)
    if err:
        return jsonify({"ok": False, "error": err}), 400
    (BASE_DIR / "config.json").write_text(
        json.dumps(cfg, indent=2, ensure_ascii=False)
    )
    _reschedule()
    return jsonify({"ok": True})


# ── API: Ticker-Autocomplete ──────────────────────────────────────────────────

@app.route("/sentiment/api/tickers")
def api_tickers():
    q = request.args.get("q", "").upper().strip()
    if len(q) < 1:
        return jsonify([])
    results = []
    try:
        with open(BASE_DIR / "tickers.csv", newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                ticker = row.get("ticker", "").strip()
                name = row.get("name", "").strip()
                if ticker.startswith(q) or q in name.upper():
                    results.append({"ticker": ticker, "name": name})
                    if len(results) >= 10:
                        break
    except Exception:
        pass
    return jsonify(results)


# ── API: Portfolio ────────────────────────────────────────────────────────────

@app.route("/sentiment/api/portfolio", methods=["GET"])
def api_portfolio_get():
    return jsonify(_load_portfolio())


_TICKER_RE = re.compile(r"^[A-Z0-9.\-]{1,10}$")


@app.route("/sentiment/api/portfolio", methods=["POST"])
def api_portfolio_add():
    body = request.get_json(force=True)
    ticker = body.get("ticker", "").strip().upper()
    if not ticker:
        return jsonify({"error": "ticker fehlt"}), 400
    if not _TICKER_RE.match(ticker):
        # Verhindert u.a. dass Sonderzeichen (Anführungszeichen etc.) über den
        # Ticker in onclick-Handler im Frontend landen (M6, XSS-Härtung)
        return jsonify({"error": "ticker ungültig"}), 400

    portfolio = _load_portfolio()

    # Duplikat prüfen
    if any(p["ticker"] == ticker for p in portfolio):
        return jsonify({"error": "Ticker bereits im Portfolio"}), 409

    try:
        shares = float(body.get("shares", 0))
        buy_price = float(body.get("buy_price", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "shares/buy_price müssen numerisch sein"}), 400

    entry = {
        "ticker": ticker,
        "name": body.get("name", ""),
        "shares": shares,
        "buy_price": buy_price,
        "buy_date": body.get("buy_date", ""),
        "last_sentiment": None,
        "current_price": None,
        "current_value": None,
        "pnl": None,
        "pnl_pct": None,
        "sell_signal": False,
        "sell_reason": None,
    }
    portfolio.append(entry)
    _save_portfolio(portfolio)

    # Sofort Quote + Sentiment holen (Hintergrundthread)
    def _init():
        from scanner import run_portfolio_scan
        run_portfolio_scan()
    threading.Thread(target=_init, daemon=True).start()

    return jsonify({"ok": True, "entry": entry}), 201


@app.route("/sentiment/api/portfolio/<ticker>", methods=["DELETE"])
def api_portfolio_delete(ticker: str):
    ticker = ticker.upper()
    portfolio = _load_portfolio()
    before = len(portfolio)
    portfolio = [p for p in portfolio if p["ticker"] != ticker]
    if len(portfolio) == before:
        return jsonify({"error": "Nicht gefunden"}), 404
    _save_portfolio(portfolio)
    return jsonify({"ok": True})


@app.route("/sentiment/api/portfolio/<ticker>", methods=["PATCH"])
def api_portfolio_update(ticker: str):
    """Sell-Signal manuell zurücksetzen."""
    ticker = ticker.upper()
    body = request.get_json(force=True)
    portfolio = _load_portfolio()
    for p in portfolio:
        if p["ticker"] == ticker:
            if "sell_signal" in body:
                p["sell_signal"] = bool(body["sell_signal"])
                p["sell_reason"] = None
            _save_portfolio(portfolio)
            return jsonify({"ok": True})
    return jsonify({"error": "Nicht gefunden"}), 404


@app.route("/sentiment/api/costs")
def api_costs():
    path = BASE_DIR / "claude_costs.json"
    if not path.exists():
        return jsonify({
            "total_cost_eur": 0.0, "total_cost_usd": 0.0,
            "total_input_tokens": 0, "total_output_tokens": 0,
            "last_threshold_notified": 0, "scans": [],
        })
    return Response(path.read_text(), mimetype="application/json")


@app.route("/sentiment/api/status")
def api_status():
    jobs = [
        {
            "id": j.id,
            "next_run": j.next_run_time.isoformat() if j.next_run_time else None,
        }
        for j in scheduler.get_jobs()
    ]
    return jsonify({"jobs": jobs})


@app.route("/sentiment/api/early-signals")
def api_early_signals():
    from signals_db import get_conn
    with get_conn() as conn:
        signals = [dict(r) for r in conn.execute(
            "SELECT ticker, signal_type, signal_ts, score, details_json FROM signals "
            "ORDER BY signal_ts DESC LIMIT 100")]
        alerts = [dict(r) for r in conn.execute(
            "SELECT * FROM alerts ORDER BY alert_ts DESC LIMIT 50")]
        stats = [dict(r) for r in conn.execute(
            "SELECT horizon_days, COUNT(*) n, AVG(ret_pct) avg_ret, "
            "SUM(CASE WHEN ret_pct > 0 THEN 1 ELSE 0 END)*100.0/COUNT(*) hit_rate "
            "FROM forward_returns WHERE ret_pct IS NOT NULL GROUP BY horizon_days")]
    return jsonify({"signals": signals, "alerts": alerts, "stats": stats})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5005, debug=False)
