"""Layer 5: SEC Schedule 13D/13G — Großaktionärs-/Aktivisten-Meldungen (>5% Anteil).

13D = aktiver Investor (kann Einfluss/Kontrolle anstreben, z.B. Board-Sitz fordern)
      -> stärkeres Signal, muss binnen 10 Tagen nach Überschreiten der 5%-Schwelle
      eingereicht werden.
13G = passiver Investor (Index-/Institutionsfonds ohne Kontrollabsicht)
      -> schwächeres Signal, häufiger (z.B. jeder große Indexfonds der zukauft).

SEC hat beide Formulare inzwischen auf strukturiertes XML umgestellt (primary_doc.xml,
analog Form 4) – verifiziert 2026-07-08 gegen echte Filings (Marchex-13D, Accuray-13G).
WICHTIG: 13D und 13G nutzen UNTERSCHIEDLICHE XML-Schemas/Tag-Namen
(issuerCIK vs issuerCik, percentOfClass vs classPercent, ...) – deshalb
namespace-/schema-unabhängige Suche per lokalem Tag-Namen (_local()) statt fixer XPath.
"""
import logging
from datetime import datetime, timezone
from urllib.parse import quote
import xml.etree.ElementTree as ET

from layer1_edgar import SEC_HEADERS, MAX_PAGES, _sec_get, _normalize_ts, _market_cap_ok, _load_universe
from signals_db import get_conn, insert_signal

log = logging.getLogger("scanner")

ATOM_NS = {"a": "http://www.w3.org/2005/Atom"}
FEED_URL = ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent"
            "&type={form}&company=&dateb=&owner=include&count=100&start={start}&output=atom")
TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"

_ticker_cache: dict = {"data": None, "loaded_at": None}


def _local(el, tag: str):
    """Erstes Element mit gegebenem lokalen Tag-Namen (namespace-unabhängig) –
    13D/13G nutzen unterschiedliche XML-Namespaces UND unterschiedliche
    Tag-Namen für denselben Sachverhalt (issuerCIK vs issuerCik), robuster
    als exakte XPath-Pfade."""
    for e in el.iter():
        if e.tag.split("}")[-1] == tag:
            return e
    return None


def _cik_to_ticker(cik: int) -> str | None:
    """CIK->Ticker über SECs offizielle company_tickers.json (frei, kein Auth),
    im Speicher gecacht (1x/Tag neu geladen, ~1MB)."""
    global _ticker_cache
    now = datetime.now(timezone.utc)
    stale = (_ticker_cache["loaded_at"] is None or
             (now - _ticker_cache["loaded_at"]).total_seconds() > 86400)
    if _ticker_cache["data"] is None or stale:
        try:
            r = _sec_get(TICKER_MAP_URL)
            data = r.json()
            _ticker_cache["data"] = {v["cik_str"]: v["ticker"] for v in data.values()}
            _ticker_cache["loaded_at"] = now
        except Exception as e:
            log.warning("Ticker-Map (company_tickers.json) laden fehlgeschlagen: %s", e)
            if _ticker_cache["data"] is None:
                return None
    return _ticker_cache["data"].get(cik)


def _feed_entries(start: int, form: str) -> list[dict]:
    """form: 'SC 13D' oder 'SC 13G'. Filtert exakt auf den Category-Term –
    sonst matchen auch '.../A'-Amendments (gleicher Pitfall wie bei Form 4)."""
    url = FEED_URL.format(form=quote(form), start=start)
    root = ET.fromstring(_sec_get(url).content)
    out = []
    for entry in root.findall("a:entry", ATOM_NS):
        cat_el = entry.find("a:category", ATOM_NS)
        if cat_el is None or cat_el.get("term") != form:
            continue
        link_el = entry.find("a:link", ATOM_NS)
        id_el = entry.find("a:id", ATOM_NS)
        updated_el = entry.find("a:updated", ATOM_NS)
        if link_el is None or id_el is None:
            continue
        acc = (id_el.text or "").rsplit("=", 1)[-1]
        out.append({
            "accession": acc,
            "index_url": link_el.get("href"),
            "updated": (updated_el.text or "").strip() if updated_el is not None else None,
        })
    return out


def _fetch_filing_xml(index_url: str) -> ET.Element | None:
    """Vom Filing-Index das primary_doc.xml finden und parsen (analog Form 4)."""
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
        if root.tag.split("}")[-1] == "edgarSubmission":
            return root
    return None


def _local_first(root: ET.Element, *tags: str):
    """Wie _local(), aber probiert mehrere Tag-Namen der Reihe nach (13D/13G
    nutzen unterschiedliche Tag-Namen für denselben Sachverhalt). WICHTIG:
    kein `a or b` mit ElementTree-Elementen – ein Element ohne Kind-Elemente
    (z.B. ein reines Text-Blatt wie <issuerCIK>) ist 'falsy', `or` würde also
    fälschlich zum Fallback springen, obwohl das erste Element gültig war."""
    for tag in tags:
        el = _local(root, tag)
        if el is not None:
            return el
    return None


def _parse_filing(root: ET.Element) -> dict | None:
    """Extrahiert Issuer-CIK, ersten Reporting-Person-Namen + Anteil/Aktienzahl.
    Bei mehreren gemeinsamen Meldern (z.B. Fondsfamilie) wird nur der erste
    genommen – für ein Frühsignal ausreichend, keine vollständige Offenlegung."""
    issuer_cik_el = _local_first(root, "issuerCIK", "issuerCik")
    if issuer_cik_el is None or not issuer_cik_el.text:
        return None
    try:
        issuer_cik = int(issuer_cik_el.text)
    except ValueError:
        return None

    name_el = _local(root, "reportingPersonName")
    owner = (name_el.text or "").strip() if name_el is not None and name_el.text else ""

    pct_el = _local_first(root, "percentOfClass", "classPercent")
    pct = None
    if pct_el is not None and pct_el.text:
        try:
            pct = float(pct_el.text)
        except ValueError:
            pct = None

    shares_el = _local_first(root, "aggregateAmountOwned",
                              "reportingPersonBeneficiallyOwnedAggregateNumberOfShares")
    shares = None
    if shares_el is not None and shares_el.text:
        try:
            shares = float(shares_el.text)
        except ValueError:
            shares = None

    return {"issuer_cik": issuer_cik, "owner": owner, "pct": pct, "shares": shares}


def run_ownership_scan(cfg: dict) -> None:
    universe = _load_universe()
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    new_count, hit_count = 0, 0

    with get_conn() as conn:
        seen = {r["accession_no"] for r in conn.execute("SELECT accession_no FROM edgar_seen")}

    for form, form_type_label, base_score in (("SC 13D", "13D", 3.0), ("SC 13G", "13G", 1.5)):
        for page in range(MAX_PAGES):
            try:
                entries = _feed_entries(page * 100, form)
            except Exception as e:
                log.warning("Ownership-Feed Seite %d (%s): %s", page, form, e)
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
                    root = _fetch_filing_xml(e["index_url"])
                    if root is None:
                        continue
                    parsed = _parse_filing(root)
                except Exception as exc:
                    log.warning("Ownership %s: %s", e["accession"], exc)
                    continue
                if parsed is None:
                    continue

                ticker = _cik_to_ticker(parsed["issuer_cik"])
                if not ticker or ticker not in universe:
                    continue
                if not _market_cap_ok(ticker, cfg):
                    continue

                signal_ts = _normalize_ts(e.get("updated"), now_iso)
                details = {
                    "owner": parsed["owner"], "pct": parsed["pct"], "shares": parsed["shares"],
                    "form_type": form_type_label, "filing_url": e["index_url"],
                }
                insert_signal(ticker, "large_holder", signal_ts, base_score, details)
                hit_count += 1
                log.info("Großaktionär: %s %s %s%% (%s)", ticker, parsed["owner"],
                          parsed["pct"], form_type_label)

            if page_all_seen:
                break

    log.info("Ownership-Lauf fertig: %d neue Filings, %d Signale", new_count, hit_count)
