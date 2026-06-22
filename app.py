import os
import json
import logging
import threading
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


# ── Config + Scheduler ───────────────────────────────────────────────────────

def _load_cfg() -> dict:
    return json.loads((BASE_DIR / "config.json").read_text())


scheduler = BackgroundScheduler()


def _reschedule():
    cfg = _load_cfg()
    scheduler.remove_all_jobs()
    for t in cfg.get("scan_times_utc", []):
        h, m = map(int, t.split(":"))
        scheduler.add_job(
            _do_scan, "cron",
            hour=h, minute=m, day_of_week="mon-fri",
            id=f"scan_{h:02d}{m:02d}",
        )
    log.info("Scan-Zeiten: %s (Mo–Fr UTC)", cfg.get("scan_times_utc"))


def _do_scan():
    from scanner import run_scan
    try:
        run_scan(_load_cfg())
    except Exception:
        log.exception("Scan-Fehler")


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


# ── API ───────────────────────────────────────────────────────────────────────

@app.route("/sentiment/api/results")
def api_results():
    path = BASE_DIR / "results.json"
    if not path.exists():
        return jsonify({"error": "Noch kein Scan durchgeführt"}), 404
    return Response(path.read_text(), mimetype="application/json")


@app.route("/sentiment/api/config", methods=["GET"])
def api_config_get():
    return jsonify(_load_cfg())


@app.route("/sentiment/api/config", methods=["POST"])
def api_config_set():
    cfg = request.get_json(force=True)
    (BASE_DIR / "config.json").write_text(json.dumps(cfg, indent=2, ensure_ascii=False))
    _reschedule()
    return jsonify({"ok": True})


@app.route("/sentiment/api/scan", methods=["POST"])
def api_scan_trigger():
    threading.Thread(target=_do_scan, daemon=True).start()
    return jsonify({"ok": True, "message": "Scan gestartet"})


@app.route("/sentiment/api/status")
def api_status():
    jobs = [
        {"id": j.id, "next_run": j.next_run_time.isoformat() if j.next_run_time else None}
        for j in scheduler.get_jobs()
    ]
    return jsonify({"jobs": jobs})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5005, debug=False)
