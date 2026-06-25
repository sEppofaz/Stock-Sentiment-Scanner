import os
import csv
import json
import logging
import threading
from datetime import datetime
from pathlib import Path
from flask import Flask, jsonify, request, send_file, Response
from apscheduler.schedulers.background import BackgroundScheduler

BASE_DIR = Path(__file__).parent
ICONS_DIR = BASE_DIR / "icons"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(BASE_DIR / "scan.log"),
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

def _market_open() -> bool:
    """True wenn NYSE/NASDAQ geöffnet (Mo–Fr 14:30–21:00 UTC)."""
    now = datetime.utcnow()
    if now.weekday() >= 5:
        return False
    t = now.hour * 60 + now.minute
    return 870 <= t <= 1260  # 14:30=870, 21:00=1260


scheduler = BackgroundScheduler()


def _reschedule():
    cfg = _load_cfg()
    scheduler.remove_all_jobs()

    # Volle Scans (aus config)
    for t in cfg.get("scan_times_utc", []):
        h, m = map(int, t.split(":"))
        scheduler.add_job(
            _do_full_scan, "cron",
            hour=h, minute=m, day_of_week="mon-fri",
            id=f"scan_{h:02d}{m:02d}",
        )

    # Portfolio-Scan: alle 15 Min Mo–Fr 14:00–21:45 UTC (market_open() als Guard)
    scheduler.add_job(
        _do_portfolio_scan, "cron",
        hour="14-21", minute="0,15,30,45", day_of_week="mon-fri",
        id="portfolio_scan",
    )

    log.info(
        "Scan-Zeiten: %s (Mo–Fr UTC) + Portfolio-Scan alle 15 Min 14:00–21:45 UTC",
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
    from scanner import SCAN_STATUS
    if SCAN_STATUS.get("running"):
        return jsonify({"ok": False, "message": "Scan läuft bereits"}), 409
    threading.Thread(target=_do_full_scan, daemon=True).start()
    return jsonify({"ok": True, "message": "Scan gestartet"})


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


@app.route("/sentiment/api/portfolio", methods=["POST"])
def api_portfolio_add():
    body = request.get_json(force=True)
    ticker = body.get("ticker", "").strip().upper()
    if not ticker:
        return jsonify({"error": "ticker fehlt"}), 400

    portfolio = _load_portfolio()

    # Duplikat prüfen
    if any(p["ticker"] == ticker for p in portfolio):
        return jsonify({"error": "Ticker bereits im Portfolio"}), 409

    entry = {
        "ticker": ticker,
        "name": body.get("name", ""),
        "shares": float(body.get("shares", 0)),
        "buy_price": float(body.get("buy_price", 0)),
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


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5005, debug=False)
