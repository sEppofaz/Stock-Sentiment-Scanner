"""Layer 1: SEC EDGAR Form 4 — Insider-Open-Market-Käufe (EARLY_SIGNALS_UMSETZUNG.md)."""
import json
import logging
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import requests

from signals_db import get_conn, insert_signal

log = logging.getLogger("scanner")

SEC_HEADERS = {"User-Agent": "Josef Fischer josef.jf.fischer@me.com"}
FEED_URL = ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent"
            "&type=4&company=&dateb=&owner=include&count=100&start={start}&output=atom")
ATOM_NS = {"a": "http://www.w3.org/2005/Atom"}
MAX_PAGES = 5          # max 500 Filings pro Lauf
REQ_DELAY = 0.15       # SEC-Limit 10 req/s → konservativ ~6/s


def _normalize_ts(raw: str | None, fallback: str) -> str:
    """Parst einen SEC-Atom-'updated'-Zeitstempel (beliebiger UTC-Offset) auf
    dasselbe Format wie now_iso (UTC, Sekunden-Präzision) – sonst driften
    signal_ts-Vergleiche (datetime('now','-7 days')) mit gemischten Offsets."""
    if not raw:
        return fallback
    try:
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat(timespec="seconds")
    except ValueError:
        return fallback


def _sec_get(url: str) -> requests.Response:
    time.sleep(REQ_DELAY)
    r = requests.get(url, headers=SEC_HEADERS, timeout=30)
    r.raise_for_status()
    return r


def _load_universe() -> set[str]:
    from scanner import _load_tickers
    return {t["ticker"].upper() for t in _load_tickers()}


def _feed_entries(start: int) -> list[dict]:
    """Eine Feed-Seite laden. Rückgabe: [{'accession': ..., 'index_url': ...}]

    Achtung: type=4 im Feed matcht per Präfix auch 424B3, 425 etc. –
    daher zusätzlich auf category term == "4" filtern (echtes Form 4, keine /A-Amendments).
    """
    root = ET.fromstring(_sec_get(FEED_URL.format(start=start)).content)
    out = []
    for entry in root.findall("a:entry", ATOM_NS):
        cat_el = entry.find("a:category", ATOM_NS)
        if cat_el is None or cat_el.get("term") != "4":
            continue
        link_el = entry.find("a:link", ATOM_NS)
        id_el = entry.find("a:id", ATOM_NS)
        updated_el = entry.find("a:updated", ATOM_NS)
        if link_el is None or id_el is None:
            continue
        # id endet auf 'accession-number=0001234567-26-000123'
        acc = (id_el.text or "").rsplit("=", 1)[-1]
        out.append({
            "accession": acc,
            "index_url": link_el.get("href"),
            # Filing-Zeitstempel (falls vorhanden) statt eines pro Lauf geteilten
            # now_iso – sonst kollidieren zwei Insider-Käufe desselben Tickers im
            # selben 15-Min-Lauf am UNIQUE(ticker, signal_type, signal_ts) und das
            # zweite Signal wird von INSERT OR IGNORE stumm verworfen (M5).
            "updated": (updated_el.text or "").strip() if updated_el is not None else None,
        })
    return out


def _fetch_form4_xml(index_url: str) -> ET.Element | None:
    """Vom Filing-Index das Form-4-XML finden und parsen."""
    dir_url = index_url.rsplit("/", 1)[0]
    listing = _sec_get(dir_url + "/index.json").json()
    for item in listing.get("directory", {}).get("item", []):
        name = item.get("name", "")
        if not name.lower().endswith(".xml"):
            continue
        try:
            root = ET.fromstring(_sec_get(f"{dir_url}/{name}").content)
        except ET.ParseError:
            continue
        if root.tag == "ownershipDocument":
            return root
    return None


def _txt(el, path, default=""):
    found = el.find(path)
    return (found.text or default).strip() if found is not None and found.text else default


def _parse_form4(root: ET.Element) -> dict | None:
    """Extrahiert Kauf-Transaktionen (Code P). None wenn kein Kauf enthalten."""
    symbol = _txt(root, ".//issuer/issuerTradingSymbol").upper()
    if not symbol:
        return None
    owner = _txt(root, ".//reportingOwner/reportingOwnerId/rptOwnerName")
    is_director = _txt(root, ".//reportingOwnerRelationship/isDirector") in ("1", "true")
    is_officer = _txt(root, ".//reportingOwnerRelationship/isOfficer") in ("1", "true")
    officer_title = _txt(root, ".//reportingOwnerRelationship/officerTitle")

    total_usd = 0.0
    total_shares = 0.0
    for tx in root.findall(".//nonDerivativeTable/nonDerivativeTransaction"):
        code = _txt(tx, ".//transactionCoding/transactionCode")
        acq = _txt(tx, ".//transactionAmounts/transactionAcquiredDisposedCode/value")
        if code != "P" or acq != "A":
            continue
        try:
            shares = float(_txt(tx, ".//transactionAmounts/transactionShares/value", "0") or 0)
            price = float(_txt(tx, ".//transactionAmounts/transactionPricePerShare/value", "0") or 0)
        except ValueError:
            continue
        total_shares += shares
        total_usd += shares * price

    if total_usd <= 0:
        return None
    return {
        "symbol": symbol, "owner": owner, "is_director": is_director,
        "is_officer": is_officer, "officer_title": officer_title,
        "total_usd": round(total_usd, 2), "total_shares": total_shares,
    }


def _market_cap_ok(symbol: str, cfg: dict) -> bool:
    """MarketCap-Filter über Finnhub /stock/metric. Achtung: Wert in MILLIONEN USD."""
    from scanner import _fh_get
    try:
        metric = _fh_get("/stock/metric", {"symbol": symbol, "metric": "all"})
        cap_mio = (metric.get("metric") or {}).get("marketCapitalization")
        if cap_mio is None:
            return True  # keine Daten → nicht wegfiltern
        cap_usd = cap_mio * 1_000_000
        f = cfg.get("filter", {})
        return f.get("market_cap_min_usd", 0) <= cap_usd <= f.get("market_cap_max_usd", 10**13)
    except Exception as e:
        log.warning("EDGAR %s marketcap: %s", symbol, e)
        return True


def run_edgar_scan(cfg: dict) -> None:
    es_cfg = cfg.get("early_signals", {})
    min_usd = es_cfg.get("insider_min_usd", 25000)
    universe = _load_universe()
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    new_count, hit_count = 0, 0

    with get_conn() as conn:
        seen = {r["accession_no"] for r in conn.execute("SELECT accession_no FROM edgar_seen")}

    for page in range(MAX_PAGES):
        try:
            entries = _feed_entries(page * 100)
        except Exception as e:
            log.warning("EDGAR Feed Seite %d: %s", page, e)
            break
        if not entries:
            break
        page_all_seen = all(e["accession"] in seen for e in entries)

        for e in entries:
            if e["accession"] in seen:
                continue
            seen.add(e["accession"])
            new_count += 1
            with get_conn() as conn:
                conn.execute("INSERT OR IGNORE INTO edgar_seen VALUES (?, ?)",
                             (e["accession"], now_iso))
            try:
                root = _fetch_form4_xml(e["index_url"])
                if root is None:
                    continue
                buy = _parse_form4(root)
            except Exception as exc:
                log.warning("EDGAR %s: %s", e["accession"], exc)
                continue
            if buy is None or buy["symbol"] not in universe or buy["total_usd"] < min_usd:
                continue
            if not _market_cap_ok(buy["symbol"], cfg):
                continue

            # Cluster-Check: ≥2 verschiedene Insider desselben Tickers in 7 Kalendertagen
            with get_conn() as conn:
                others = conn.execute(
                    "SELECT details_json FROM signals WHERE ticker=? AND signal_type='insider_buy' "
                    "AND signal_ts >= strftime('%Y-%m-%dT%H:%M:%S', 'now', '-7 days')", (buy["symbol"],)).fetchall()
            other_owners = {json.loads(r["details_json"]).get("owner") for r in others}
            cluster = len(other_owners - {buy["owner"]}) >= 1

            score = 3.0 + (2.0 if cluster else 0.0)
            details = dict(buy)
            details["cluster"] = cluster
            details["filing_url"] = e["index_url"]
            signal_ts = _normalize_ts(e.get("updated"), now_iso)
            insert_signal(buy["symbol"], "insider_buy", signal_ts, score, details)
            hit_count += 1
            log.info("EDGAR Insider-Kauf: %s %s %.0f USD (cluster=%s)",
                     buy["symbol"], buy["owner"], buy["total_usd"], cluster)

        if page_all_seen:
            break

    log.info("EDGAR-Lauf fertig: %d neue Filings, %d Kaufsignale", new_count, hit_count)
