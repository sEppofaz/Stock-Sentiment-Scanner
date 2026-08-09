import functools
import os
import csv
import json
import logging
import logging.handlers
import re
import threading
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
from flask import Flask, jsonify, redirect, request, send_file, session, Response
from apscheduler.schedulers.background import BackgroundScheduler

BASE_DIR = Path(__file__).parent
ICONS_DIR = BASE_DIR / "icons"

# ── Login (Flask-Session, ADR-015) ──────────────────────────────────────────
# nginx Basic-Auth wurde zuerst versucht und live wieder verworfen: die
# installierte Home-Bildschirm-PWA zeigte den nativen Basic-Auth-Dialog nicht
# an (Service Worker fängt die Navigation per fetch() ab, ein 401 löst dabei
# keinen Browser-Prompt aus – anders als ein direkter Top-Level-Request).
# Session-Cookies + normale HTTP-Redirects funktionieren dagegen auch
# innerhalb des Service-Worker-Fetch-Handlers zuverlässig. Exakt das Muster,
# das Claude Remote bereits nutzt (app.secret_key, HtpasswdFile-Check).
_SESSION_KEY_FILE = BASE_DIR / ".session_key"
_HTPASSWD_FILE = "/etc/nginx/sentiment.htpasswd"


def _get_or_create_session_key() -> str:
    if _SESSION_KEY_FILE.exists():
        return _SESSION_KEY_FILE.read_text().strip()
    import secrets as _sec
    key = _sec.token_hex(32)
    _SESSION_KEY_FILE.write_text(key)
    _SESSION_KEY_FILE.chmod(0o600)
    return key

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
app.secret_key = _get_or_create_session_key()
app.permanent_session_lifetime = timedelta(days=30)


@app.before_request
def _refresh_session():
    # Sliding Window: jede Anfrage eines eingeloggten Users verlängert die
    # Session um weitere 30 Tage (bewusst länger als Claude Remotes 15 Min –
    # dort geht es um SSH-/Server-Eingriffe, hier nur um Portfolio-Einsicht).
    if session.get("authenticated"):
        session.modified = True


def _check_password(password: str) -> bool:
    try:
        from passlib.apache import HtpasswdFile
        ht = HtpasswdFile(_HTPASSWD_FILE)
        users = list(ht.users())
        if not users:
            return False
        return bool(ht.check_password(users[0], password))
    except Exception:
        return False


def login_required(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("authenticated"):
            return redirect("/sentiment/login")
        return f(*args, **kwargs)
    return decorated


@app.route("/sentiment/login", methods=["GET", "POST"])
def login():
    if session.get("authenticated"):
        return redirect("/sentiment/")
    if request.method == "POST":
        if _check_password(request.form.get("password", "")):
            session.permanent = True
            session["authenticated"] = True
            return redirect("/sentiment/")
        return redirect("/sentiment/login?error=1")
    return send_file(BASE_DIR / "pwa" / "login.html")


@app.route("/sentiment/logout")
def logout():
    session.clear()
    return redirect("/sentiment/login")


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

    wa = cfg.get("weekly_analysis", {})
    if "interval_days" in wa and (not isinstance(wa["interval_days"], (int, float)) or wa["interval_days"] < 1):
        return "weekly_analysis.interval_days muss eine Zahl >= 1 sein"

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
    # Minute 12/27/42/57 statt 0/15/30/45 (Josef-Feedback 2026-07-08): sonst
    # konkurriert der Finnhub-intensive Portfolio-Scan mit edgar_scan/
    # ownership_scan/es_instant um denselben globalen 55-Calls/Min-Throttle,
    # da alle vier zur selben Minute ausgelöst würden. Siehe Staffelung unten.
    scheduler.add_job(
        _do_portfolio_scan, "cron",
        hour="9-16", minute="12,27,42,57", day_of_week="mon-fri",
        timezone="America/New_York", id="portfolio_scan",
    )

    # Tägliches Cleanup alter buzz_history/edgar_seen-Zeilen (M8) – unabhängig
    # von early_signals.enabled, da buzz_history immer aus dem Vollscan befüllt wird
    scheduler.add_job(
        _do_cleanup, "cron", hour=3, minute=0, timezone="UTC", id="daily_cleanup",
    )

    # Scan-Snapshot-Tracker (wöchentliche Performance-Analyse, Sentiment-Scan-Teil):
    # 15 Min nach es_tracker (17:45 ET), keine Finnhub-Konkurrenz da nur yfinance
    scheduler.add_job(
        _do_scan_tracker, "cron", hour=18, minute=0,
        day_of_week="mon-fri", timezone="America/New_York", id="scan_tracker",
    )

    # Performance-Analyse: läuft täglich (Mo-Fr 18:10 ET, direkt nach den
    # beiden Tracker-Jobs scan_tracker/es_tracker um 18:00/17:45 ET, die die
    # frischen Kursrenditen nachtragen – vorher hätte die Analyse mit
    # veralteten Daten gerechnet), entscheidet aber intern per
    # weekly_analysis.interval_days selbst, ob sie an einem gegebenen Tag
    # tatsächlich läuft (Josef-Wunsch 2026-08-09: Intervall einstellbar, ohne
    # dass der Scheduler-Job selbst bei jeder Config-Änderung neu gebaut
    # werden muss). Trotz des Namens "weekly_analysis" (Job-ID/Config-Block
    # aus Kompatibilitätsgründen unverändert) läuft der Job seither täglich.
    if cfg.get("weekly_analysis", {}).get("enabled", True):
        scheduler.add_job(
            _do_weekly_analysis, "cron", hour=18, minute=10,
            day_of_week="mon-fri", timezone="America/New_York", id="weekly_analysis",
        )

    # Frühsignale (EARLY_SIGNALS_UMSETZUNG.md)
    if cfg.get("early_signals", {}).get("enabled", False):
        # Die vier 15-Min-Jobs (edgar/ownership/es_instant/portfolio_scan) sind
        # bewusst in ~4-Min-Schritten gestaffelt (Josef-Feedback 2026-07-08) –
        # vorher liefen alle zur selben Minute und konkurrierten um denselben
        # globalen Finnhub-Throttle (scanner._throttle(), 55 Calls/Min).
        scheduler.add_job(
            _do_edgar_scan, "cron",
            hour="6-22", minute="0,15,30,45", day_of_week="mon-fri",
            timezone="America/New_York", id="edgar_scan",
        )
        # Layer 5: SC 13D/13G Großaktionärs-Meldungen (2026-07-08, Josef-Feedback)
        scheduler.add_job(
            _do_ownership_scan, "cron",
            hour="6-22", minute="4,19,34,49", day_of_week="mon-fri",
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
            hour="6-22", minute="8,23,38,53", day_of_week="mon-fri",
            timezone="America/New_York", id="es_instant",
        )
        # Layer 6: Tages-Konsolidierung (Top-1-Pick) – nach scan_tracker (18:00),
        # damit sowohl ein heutiger Vollscan als auch alle Frühsignal-Alerts des
        # Tages als Kandidaten vorliegen
        scheduler.add_job(
            _do_daily_pick, "cron", hour=18, minute=5,
            day_of_week="mon-fri", timezone="America/New_York", id="daily_pick",
        )
        # Verkaufssignal-Check: Minuten-Raster Rest 1 mod 5 (Fable-Fix #3) – der
        # einzige freie Slot ggü. edgar_scan(0)/portfolio_scan(2)/es_instant(3)/
        # ownership_scan(4), NICHT */5 (kollidiert garantiert mit edgar_scan)
        scheduler.add_job(
            _do_sell_signal_check, "cron",
            hour="9-16", minute="1,6,11,16,21,26,31,36,41,46,51,56", day_of_week="mon-fri",
            timezone="America/New_York", id="sell_signal_check",
        )

    log.info(
        "Scan-Zeiten: %s (Mo–Fr UTC) + Portfolio-Scan alle 15 Min :12/:27/:42/:57 America/New_York (gestaffelt ggü. EDGAR-Jobs)",
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


def _do_portfolio_scan(force: bool = False):
    if not _load_cfg().get("scan_enabled", True):
        return
    if not force and not _market_open():
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


def _do_daily_pick():
    cfg = _load_cfg()
    if not cfg.get("early_signals", {}).get("enabled", False):
        return
    try:
        from layer6_daily_pick import run_daily_pick
        run_daily_pick(cfg)
    except Exception:
        log.exception("Tages-Pick fehlgeschlagen")


def _do_sell_signal_check():
    cfg = _load_cfg()
    if not cfg.get("early_signals", {}).get("enabled", False):
        return
    try:
        from layer6_sell_signal import check_frühsignal_sell_exits
        check_frühsignal_sell_exits(cfg)
    except Exception:
        log.exception("Verkaufssignal-Check fehlgeschlagen")


def _do_scan_tracker():
    if not _load_cfg().get("scan_enabled", True):
        return
    try:
        from scan_tracker import run_scan_tracker
        run_scan_tracker(_load_cfg())
    except Exception:
        log.exception("Scan-Tracker fehlgeschlagen")


def _do_weekly_analysis():
    cfg = _load_cfg()
    if not cfg.get("weekly_analysis", {}).get("enabled", True):
        return
    try:
        from weekly_analysis import run_weekly_analysis
        run_weekly_analysis(cfg)
    except Exception:
        log.exception("Wöchentliche Analyse fehlgeschlagen")


def _do_cleanup():
    try:
        from signals_db import cleanup_old_data
        buzz_deleted, edgar_deleted = cleanup_old_data()
        log.info("Cleanup: %d buzz_history- + %d edgar_seen-Zeilen entfernt (>60/>30 Tage)",
                  buzz_deleted, edgar_deleted)
    except Exception:
        log.exception("Cleanup fehlgeschlagen")


from signals_db import init_db
import costs
init_db()

scheduler.start()
_reschedule()

# ── PWA-Dateien ───────────────────────────────────────────────────────────────

@app.route("/sentiment/")
@login_required
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
@login_required
def api_results():
    path = BASE_DIR / "results.json"
    if not path.exists():
        return jsonify({"error": "Noch kein Scan durchgeführt"}), 404
    return Response(path.read_text(), mimetype="application/json")


@app.route("/sentiment/api/scan", methods=["POST"])
@login_required
def api_scan_trigger():
    if datetime.utcnow().weekday() >= 5:
        return jsonify({"ok": False, "message": "Kein Scan am Wochenende"}), 409
    from scanner import SCAN_STATUS
    if SCAN_STATUS.get("running"):
        return jsonify({"ok": False, "message": "Scan läuft bereits"}), 409
    threading.Thread(target=_do_full_scan, daemon=True).start()
    return jsonify({"ok": True, "message": "Scan gestartet"})


@app.route("/sentiment/api/portfolio/scan", methods=["POST"])
@login_required
def api_portfolio_scan_trigger():
    from scanner import SCAN_STATUS
    if SCAN_STATUS.get("running"):
        return jsonify({"ok": False, "message": "Scan läuft bereits"}), 409
    threading.Thread(target=_do_portfolio_scan, kwargs={"force": True}, daemon=True).start()
    return jsonify({"ok": True, "message": "Portfolio-Scan gestartet"})


@app.route("/sentiment/api/scan/status")
@login_required
def api_scan_status():
    from scanner import SCAN_STATUS
    return jsonify(SCAN_STATUS)


@app.route("/sentiment/api/scan/abort", methods=["POST"])
@login_required
def api_scan_abort():
    from scanner import SCAN_STATUS
    if SCAN_STATUS.get("running"):
        SCAN_STATUS["abort"] = True
        return jsonify({"ok": True, "message": "Abbruch angefordert"})
    return jsonify({"ok": False, "message": "Kein Scan läuft"})


# ── API: Config ───────────────────────────────────────────────────────────────

@app.route("/sentiment/api/config", methods=["GET"])
@login_required
def api_config_get():
    return jsonify(_load_cfg())


@app.route("/sentiment/api/config", methods=["POST"])
@login_required
def api_config_set():
    cfg = request.get_json(force=True)
    err = _validate_cfg(cfg)
    if err:
        return jsonify({"ok": False, "error": err}), 400
    from scanner import _update_config
    _update_config(lambda _current: cfg)
    _reschedule()
    return jsonify({"ok": True})


# ── API: Ticker-Autocomplete ──────────────────────────────────────────────────

@app.route("/sentiment/api/tickers")
@login_required
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
@login_required
def api_portfolio_get():
    return jsonify(_load_portfolio())


_TICKER_RE = re.compile(r"^[A-Z0-9.\-]{1,10}$")


@app.route("/sentiment/api/portfolio", methods=["POST"])
@login_required
def api_portfolio_add():
    body = request.get_json(force=True)
    ticker = body.get("ticker", "").strip().upper()
    if not ticker:
        return jsonify({"error": "ticker fehlt"}), 400
    if not _TICKER_RE.match(ticker):
        # Verhindert u.a. dass Sonderzeichen (Anführungszeichen etc.) über den
        # Ticker in onclick-Handler im Frontend landen (M6, XSS-Härtung)
        return jsonify({"error": "ticker ungültig"}), 400

    try:
        shares = float(body.get("shares", 0))
        buy_price = float(body.get("buy_price", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "shares/buy_price müssen numerisch sein"}), 400

    buy_date = body.get("buy_date", "")
    currency = (body.get("currency") or "USD").upper()
    if currency not in ("USD", "EUR"):
        return jsonify({"error": "currency muss 'USD' oder 'EUR' sein"}), 400
    buy_price_eur, fx_rate_used = None, None
    if currency == "EUR":
        from scanner import _eur_to_usd_rate
        fx_rate_used = _eur_to_usd_rate(buy_date or None)
        if fx_rate_used is None:
            return jsonify({"error": "EUR/USD-Kurs konnte nicht ermittelt werden – bitte später erneut versuchen oder USD eingeben"}), 400
        buy_price_eur = buy_price
        buy_price = round(buy_price * fx_rate_used, 4)

    entry = {
        "ticker": ticker,
        "name": body.get("name", ""),
        "shares": shares,
        "buy_price": buy_price,
        "buy_price_eur": buy_price_eur,
        "fx_rate_used": fx_rate_used,
        "buy_date": buy_date,
        "last_sentiment": None,
        "current_price": None,
        "current_value": None,
        "pnl": None,
        "pnl_pct": None,
        "sell_signal": False,
        "sell_reason": None,
    }

    # Duplikat-Check innerhalb des Locks (nicht nur vorab) – sonst TOCTOU-Race
    # zwischen zwei gleichzeitigen Requests für denselben Ticker
    from scanner import _update_portfolio
    duplicate = False

    def _add_mutator(cur):
        nonlocal duplicate
        if any(p["ticker"] == ticker for p in cur):
            duplicate = True
            return cur
        return cur + [entry]

    _update_portfolio(_add_mutator)
    if duplicate:
        return jsonify({"error": "Ticker bereits im Portfolio"}), 409

    # Sofort Quote + Sentiment holen (Hintergrundthread)
    def _init():
        from scanner import run_portfolio_scan
        run_portfolio_scan()
    threading.Thread(target=_init, daemon=True).start()

    return jsonify({"ok": True, "entry": entry}), 201


@app.route("/sentiment/api/portfolio/<ticker>", methods=["DELETE"])
@login_required
def api_portfolio_delete(ticker: str):
    ticker = ticker.upper()
    from scanner import _update_portfolio
    found = False

    def _del_mutator(cur):
        nonlocal found
        filtered = [p for p in cur if p["ticker"] != ticker]
        found = len(filtered) != len(cur)
        return filtered

    _update_portfolio(_del_mutator)
    if not found:
        return jsonify({"error": "Nicht gefunden"}), 404
    return jsonify({"ok": True})


@app.route("/sentiment/api/portfolio/<ticker>", methods=["PATCH"])
@login_required
def api_portfolio_update(ticker: str):
    """Sell-Signal manuell zurücksetzen."""
    ticker = ticker.upper()
    body = request.get_json(force=True)
    from scanner import _update_portfolio
    found = False

    def _upd_mutator(cur):
        nonlocal found
        for p in cur:
            if p["ticker"] == ticker:
                found = True
                if "sell_signal" in body:
                    p["sell_signal"] = bool(body["sell_signal"])
                    p["sell_reason"] = None
                    p["sell_signal_source"] = None
        return cur

    _update_portfolio(_upd_mutator)
    if not found:
        return jsonify({"error": "Nicht gefunden"}), 404
    return jsonify({"ok": True})


@app.route("/sentiment/api/portfolio/<ticker>/convert", methods=["PATCH"])
@login_required
def api_portfolio_convert(ticker: str):
    """Wandelt eine Auto-Watch-Beobachtungsposition (watch:true, 1 Test-Aktie)
    in eine echte, tatsächlich gekaufte Position um (Josef-Wunsch 2026-08-09) –
    ersetzt die Platzhalterwerte durch die tatsächlichen Kaufdaten. Optional
    currency='EUR' (Josef-Wunsch 2026-08-09): buy_price wird dann als
    EUR-Betrag interpretiert und zum Kurs des Kaufdatums in USD umgerechnet."""
    ticker = ticker.upper()
    body = request.get_json(force=True)
    try:
        shares = float(body.get("shares", 0))
        buy_price = float(body.get("buy_price", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "shares/buy_price müssen numerisch sein"}), 400
    if shares <= 0 or buy_price <= 0:
        return jsonify({"error": "shares/buy_price müssen größer 0 sein"}), 400
    buy_date = body.get("buy_date")

    currency = (body.get("currency") or "USD").upper()
    if currency not in ("USD", "EUR"):
        return jsonify({"error": "currency muss 'USD' oder 'EUR' sein"}), 400
    buy_price_eur, fx_rate_used = None, None
    if currency == "EUR":
        from scanner import _eur_to_usd_rate
        fx_rate_used = _eur_to_usd_rate(buy_date)
        if fx_rate_used is None:
            return jsonify({"error": "EUR/USD-Kurs konnte nicht ermittelt werden – bitte später erneut versuchen oder USD eingeben"}), 400
        buy_price_eur = buy_price
        buy_price = round(buy_price * fx_rate_used, 4)

    from scanner import _update_portfolio, _apply_price
    result = {"status": None}

    def _conv_mutator(cur):
        for p in cur:
            if p["ticker"] == ticker:
                if not p.get("watch"):
                    result["status"] = "not_watch"
                    return cur
                p["watch"] = False
                p["shares"] = shares
                p["buy_price"] = buy_price
                p["buy_price_eur"] = buy_price_eur
                p["fx_rate_used"] = fx_rate_used
                if buy_date:
                    p["buy_date"] = buy_date
                # Sofortige Neuberechnung mit dem letzten bekannten Kurs, statt bis
                # zum nächsten 15-Min-Portfolio-Scan falsche Werte (aus dem alten
                # 1-Aktie-Watch-Zustand) stehen zu lassen (live gefundener Bug,
                # PRQR-Beispiel 2026-08-09: Positionswert/P&L zeigten noch die
                # Zahlen von vor der Umwandlung).
                _apply_price(p, p.get("current_price"))
                result["status"] = "ok"
                return cur
        result["status"] = "not_found"
        return cur

    _update_portfolio(_conv_mutator)
    if result["status"] == "not_found":
        return jsonify({"error": "Nicht gefunden"}), 404
    if result["status"] == "not_watch":
        return jsonify({"error": "Ticker ist bereits eine echte Position"}), 400

    # Zusätzlich einen wirklich frischen Kurs nachziehen (Hintergrund, analog
    # api_portfolio_add()) – _apply_price() oben nutzt nur den letzten bekannten
    # Watch-Kurs, das kann einige Minuten alt sein.
    def _refresh():
        from scanner import run_portfolio_scan
        run_portfolio_scan()
    threading.Thread(target=_refresh, daemon=True).start()

    return jsonify({"ok": True})


# ── API: Layer 6 – Tages-Pick + Verkaufssignal-Check ───────────────────────────

@app.route("/sentiment/api/daily-pick/latest")
@login_required
def api_daily_pick_latest():
    from signals_db import get_conn
    with get_conn() as conn:
        row = conn.execute(
            "SELECT pick_date, ticker, source, reasoning_json, price_at_pick, created_ts "
            "FROM daily_picks ORDER BY pick_date DESC LIMIT 1").fetchone()
    if row is None:
        return jsonify({"exists": False})
    d = dict(row)
    d["reasoning"] = json.loads(d.pop("reasoning_json") or "{}")
    return jsonify({"exists": True, **d})


@app.route("/sentiment/api/daily-pick/run", methods=["POST"])
@login_required
def api_daily_pick_run():
    body = request.get_json(force=True, silent=True) or {}
    force = bool(body.get("force"))
    try:
        from layer6_daily_pick import run_daily_pick_manual
        result = run_daily_pick_manual(_load_cfg(), force=force)
        return jsonify({"ok": True, **result})
    except Exception:
        log.exception("Manueller Tages-Pick-Lauf fehlgeschlagen")
        return jsonify({"ok": False, "error": "Tages-Pick fehlgeschlagen"}), 500


@app.route("/sentiment/api/sell-signal-check/run", methods=["POST"])
@login_required
def api_sell_signal_check_run():
    try:
        from layer6_sell_signal import check_frühsignal_sell_exits
        check_frühsignal_sell_exits(_load_cfg())
        return jsonify({"ok": True})
    except Exception:
        log.exception("Manueller Verkaufssignal-Check fehlgeschlagen")
        return jsonify({"ok": False, "error": "Verkaufssignal-Check fehlgeschlagen"}), 500


@app.route("/sentiment/api/costs")
@login_required
def api_costs():
    path = BASE_DIR / "claude_costs.json"
    if path.exists():
        try:
            raw = json.loads(path.read_text())
        except Exception:
            raw = {}
    else:
        raw = {
            "total_cost_eur": 0.0, "total_cost_usd": 0.0,
            "total_input_tokens": 0, "total_output_tokens": 0,
            "last_threshold_notified": 0, "scans": [],
        }
    merged = {**raw, **costs.load_costs_summary()}
    return jsonify(merged)


@app.route("/sentiment/api/status")
@login_required
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
@login_required
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


# ── API: Wöchentliche Performance-Analyse ──────────────────────────────────────

@app.route("/sentiment/api/analysis/latest")
@login_required
def api_analysis_latest():
    system = request.args.get("system", "")
    if system not in ("sentiment", "early_signals", "cross_signal"):
        return jsonify({"error": "system muss 'sentiment', 'early_signals' oder 'cross_signal' sein"}), 400
    from weekly_analysis import get_latest_report
    report = get_latest_report(system)
    if report is None:
        return jsonify({"exists": False, "system": system})
    return jsonify({"exists": True, **report})


@app.route("/sentiment/api/analysis/run", methods=["POST"])
@login_required
def api_analysis_run():
    body = request.get_json(force=True, silent=True) or {}
    system = body.get("system", "")
    if system not in ("sentiment", "early_signals", "cross_signal"):
        return jsonify({"error": "system muss 'sentiment', 'early_signals' oder 'cross_signal' sein"}), 400
    try:
        from weekly_analysis import analyze_and_store
        report = analyze_and_store(_load_cfg(), system)
        return jsonify({"ok": True, **report})
    except Exception:
        log.exception("Analyse fehlgeschlagen (%s)", system)
        return jsonify({"ok": False, "error": "Analyse fehlgeschlagen"}), 500


@app.route("/sentiment/api/analysis/run-ai", methods=["POST"])
@login_required
def api_analysis_run_ai():
    body = request.get_json(force=True, silent=True) or {}
    system = body.get("system", "")
    if system not in ("sentiment", "early_signals", "cross_signal"):
        return jsonify({"error": "system muss 'sentiment', 'early_signals' oder 'cross_signal' sein"}), 400
    from weekly_analysis import get_latest_report, generate_ai_text
    report_row = get_latest_report(system)
    if report_row is None:
        return jsonify({"ok": False, "error": "Noch keine Analyse für dieses System vorhanden"}), 400
    try:
        result = generate_ai_text(report_row, system)
        return jsonify({"ok": True, **result})
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except RuntimeError as e:
        # Tages-Kostenlimit erreicht – kein 500, ist erwartbar
        return jsonify({"ok": False, "error": str(e)}), 429
    except Exception:
        log.exception("KI-Analyse fehlgeschlagen (%s)", system)
        return jsonify({"ok": False, "error": "KI-Analyse fehlgeschlagen"}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5005, debug=False)
