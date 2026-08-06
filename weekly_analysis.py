"""Wöchentliche Performance-Analyse: vergleicht Score-/Signal-Komponenten von
positiv vs. negativ performenden Empfehlungen (Kursrendite HORIZON Handelstage
später), getrennt für Sentiment-Scan und Frühsignale. Siehe ADR-009/ADR-010.

Regelbasiert und kostenlos (analyze_and_store/run_weekly_analysis). Der
optionale generate_ai_text() ist der einzige Pfad, der Kosten auslöst (Claude
Haiku, nur bei explizitem Button-Klick, nutzt den bereits berechneten Report
als Kontext statt eines neuen Datendurchlaufs)."""
import json
import logging
import math
import os
import statistics
from collections import Counter
from datetime import date, datetime, timedelta, timezone

import anthropic

import costs
from signals_db import get_conn

log = logging.getLogger("scanner")

MIN_SAMPLE = 15
HORIZON = 20  # Handelstage, auf dem die Gruppierung basiert (langfristigster Horizont)


# ── Statistik-Helfer ──────────────────────────────────────────────────────────

def _mean(vals):
    vals = [v for v in vals if v is not None]
    return round(statistics.mean(vals), 2) if vals else None


def _median(vals):
    vals = [v for v in vals if v is not None]
    return round(statistics.median(vals), 2) if vals else None


def _group_stats(rows: list[dict], value_cols: list[str]) -> dict:
    return {
        col: {"mean": _mean([r.get(col) for r in rows]),
              "median": _median([r.get(col) for r in rows])}
        for col in value_cols
    }


def _top_examples(rows: list[dict], key: str, n: int, reverse: bool) -> list[dict]:
    ordered = sorted(rows, key=lambda r: r[key], reverse=reverse)[:n]
    return [{"ticker": r["ticker"], "ret_pct": r[key]} for r in ordered]


def _split_groups(rows: list[dict], pos_thr: float, neg_thr: float) -> dict:
    """rows brauchen 'ret_pct'. Fester Schwellenwert ist stabiler interpretierbar
    über Wochen hinweg als Quartile; Quartil-Fallback nur bei zu kleiner Gruppe."""
    n = len(rows)
    pos = [r for r in rows if r["ret_pct"] > pos_thr]
    neg = [r for r in rows if r["ret_pct"] < neg_thr]
    method = "threshold"
    if len(pos) < 5 or len(neg) < 5:
        method = "quartile_fallback"
        ordered = sorted(rows, key=lambda r: r["ret_pct"])
        q = max(1, n // 4)
        neg = ordered[:q]
        pos = ordered[-q:]
    return {"pos": pos, "neg": neg, "method": method, "sample_size": n}


def _thresholds(cfg: dict, system: str) -> tuple[float, float]:
    wa = cfg.get("weekly_analysis", {})
    if system == "sentiment":
        return (wa.get("sentiment_pos_threshold_pct", 10.0),
                wa.get("sentiment_neg_threshold_pct", -10.0))
    return (wa.get("early_signals_pos_threshold_pct", 15.0),
            wa.get("early_signals_neg_threshold_pct", -15.0))


def _recent_matured_count(system: str) -> int:
    table = "scan_forward_returns" if system == "sentiment" else "forward_returns"
    cutoff = (datetime.now(timezone.utc) - timedelta(days=28)).isoformat(timespec="seconds")
    with get_conn() as conn:
        row = conn.execute(
            f"SELECT COUNT(*) c FROM {table} WHERE horizon_days=? AND ret_pct IS NOT NULL "
            f"AND filled_ts >= ?",
            (HORIZON, cutoff),
        ).fetchone()
    return row["c"]


def _eta_weeks(system: str, current_n: int) -> int | None:
    remaining = max(0, MIN_SAMPLE - current_n)
    if remaining == 0:
        return 0
    matured_4w = _recent_matured_count(system)
    if matured_4w <= 0:
        return None
    return math.ceil(remaining / (matured_4w / 4))


def _insufficient_report(system: str, n: int) -> dict:
    return {
        "system": system,
        "sample_size": n,
        "insufficient_data": True,
        "min_sample": MIN_SAMPLE,
        "eta_weeks": _eta_weeks(system, n),
    }


# ── Konkrete Anpassungsvorschläge (regelbasiert, 0 Kosten) ────────────────────
# Für jede Metrik: (Richtung laut aktueller Scoring-/Filter-Logik, betroffene
# config.json-Schwellenwerte falls vorhanden, Anzeigelabel, Score-Formel-Gewicht
# falls die Metrik direkt in _calc_score() einfließt statt über einen
# config.json-Schwellenwert gesteuert zu werden).
_SENTIMENT_LEVERS = {
    "bullish_pct": ("higher_is_better", ["filter.bullish_pct_min"], "Bullish-Anteil", "45% in _calc_score()"),
    "bearish_pct": ("lower_is_better", ["filter.bearish_pct_max"], "Bearish-Anteil", None),
    "buzz": ("higher_is_better", [], "Buzz (Artikel-Volumen)", "30% (normiert) in _calc_score()"),
    "sentiment_norm": ("higher_is_better", [], "NLP-Sentiment-Score", "25% in _calc_score()"),
    "pe": ("lower_is_better", [], "KGV", "Bonus nur bei 0<pe<30 (hartcodiert in _calc_score())"),
    "claude_confidence": ("higher_is_better", [], "Claude-Konfidenz",
                          "kein Score-Gewicht, aber Kandidat für einen neuen Mindest-Konfidenz-Filter"),
    "avg_volume_10d": ("higher_is_better", [], "Ø 10-Tage-Handelsvolumen",
                       "kein Score-Gewicht, explorativ – Richtung nicht vorab angenommen, nur beobachtet"),
}

_EARLY_LEVERS = {
    "volume_z_score": ("higher_is_better",
                        ["early_signals.volume_z_min", "early_signals.single_volume_z_min"],
                        "Volumen-z-Score", None),
    "buzz_rel_accel": ("higher_is_better",
                        ["early_signals.buzz_rel_accel_min", "early_signals.single_buzz_accel_min"],
                        "Buzz-Beschleunigung", None),
    "insider_buy_total_usd": ("higher_is_better",
                               ["early_signals.insider_min_usd", "early_signals.single_insider_min_usd"],
                               "Insider-Kaufbetrag ($)", None),
    "large_holder_pct": ("higher_is_better",
                          ["early_signals.single_large_holder_13g_min_pct"],
                          "13D/13G-Anteil (%)", None),
}


def _cfg_get(cfg: dict, dotted_key: str):
    node = cfg
    for part in dotted_key.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node


def _confidence(n_pos: int, n_neg: int) -> str:
    """Grobe Einordnung, wie ernst ein Vorschlag zu nehmen ist – KEIN echter
    Signifikanztest, nur eine simple Stichprobengrößen-Heuristik."""
    n = min(n_pos, n_neg)
    if n >= 15:
        return "hoch"
    if n >= 5:
        return "mittel"
    return "niedrig"


def _suggest_adjustments(cfg: dict, system: str, report: dict) -> list[dict]:
    """Vergleicht Mittelwerte zwischen Positiv-/Negativ-Gruppe und schlägt bei
    auffälligem Unterschied (>=20% relative Differenz) eine konkrete
    Config-Anpassung vor. Grobe Faustregel auf Basis von Mittelwerten, kein
    statistischer Test – Konfidenz (Stichprobengröße) wird immer mitgegeben.

    Wichtig: wenn die Negativ-Gruppe bei einer eigentlich 'höher=besser'
    gewerteten Metrik den höheren Wert hat (unexpected_inverse), wird KEIN
    einfacher 'Schwelle anheben'-Vorschlag gemacht – das wäre in diesem Fall
    falsch (siehe VYNE-Fall 2026-08-06: extreme Volumen-z-Scores korrelierten
    in der ersten Stichprobe mit SCHLECHTERER, nicht besserer Performance)."""
    pos, neg = report["pos_group"], report["neg_group"]
    conf = _confidence(pos["n"], neg["n"])
    levers = _SENTIMENT_LEVERS if system == "sentiment" else _EARLY_LEVERS
    suggestions = []

    for metric, (direction, config_keys, label, weight_hint) in levers.items():
        pos_stat = pos["stats"].get(metric) if system == "sentiment" else pos.get(metric)
        neg_stat = neg["stats"].get(metric) if system == "sentiment" else neg.get(metric)
        if not pos_stat or not neg_stat:
            continue
        pos_mean, neg_mean = pos_stat.get("mean"), neg_stat.get("mean")
        if pos_mean is None or neg_mean is None:
            continue

        base = max(abs(pos_mean), abs(neg_mean), 1e-9)
        rel_diff = (pos_mean - neg_mean) / base
        if abs(rel_diff) < 0.2:
            continue  # kein auffälliger Unterschied

        expected_sign = 1 if direction == "higher_is_better" else -1
        actual_sign = 1 if rel_diff > 0 else -1
        confirms = actual_sign == expected_sign

        entry = {
            "metric": metric, "label": label, "direction": direction,
            "pos_mean": pos_mean, "neg_mean": neg_mean,
            "rel_diff_pct": round(rel_diff * 100, 1),
            "config_keys": config_keys, "confidence": conf,
        }

        cmp_word = "höherer" if direction == "higher_is_better" else "niedrigerer"
        verb = "anheben" if direction == "higher_is_better" else "senken"

        if confirms and config_keys:
            suggested = round((pos_mean + neg_mean) / 2, 2)
            current_vals = {k: _cfg_get(cfg, k) for k in config_keys}
            entry["kind"] = "raise_threshold"
            entry["suggested_value"] = suggested
            entry["current_values"] = current_vals
            entry["text"] = (
                f"{label}: Positiv-Gruppe Ø {pos_mean} vs. Negativ-Gruppe Ø {neg_mean} "
                f"({cmp_word} Wert = bessere Performance, bestätigt bisherige Annahme). "
                f"Faustregel-Vorschlag: {', '.join(config_keys)} (aktuell {current_vals}) "
                f"Richtung {suggested} {verb}, um näher am Profil der Positiv-Gruppe zu filtern."
            )
        elif confirms:
            entry["kind"] = "observation_confirms"
            entry["text"] = (
                f"{label}: Positiv-Gruppe Ø {pos_mean} vs. Negativ-Gruppe Ø {neg_mean} "
                f"({cmp_word} Wert = bessere Performance). Kein config.json-Schwellenwert vorhanden – "
                + (f"Kandidat für eine Gewichts-Anpassung im Code ({weight_hint})."
                   if weight_hint else "aktuell nur Beobachtung, kein direkter Hebel.")
            )
        else:
            entry["kind"] = "unexpected_inverse"
            entry["text"] = (
                f"{label}: Negativ-Gruppe hat hier den höheren/'stärkeren' Wert (Ø {neg_mean} vs. "
                f"Ø {pos_mean} in der Positiv-Gruppe) – WIDERSPRICHT der bisherigen Annahme "
                f"'{cmp_word} Wert = stärkeres Signal'. Kein einfacher Schwellenwert-Vorschlag möglich "
                f"(bräuchte z.B. eine Obergrenze statt nur eine Untergrenze) – eher ein Hinweis, extreme "
                f"Werte hier nicht unreflektiert als Bonus zu werten."
            )

        suggestions.append(entry)

    suggestions.sort(key=lambda s: -abs(s["rel_diff_pct"]))
    return suggestions


# ── Sentiment-Scan-Analyse ─────────────────────────────────────────────────────

_SENTIMENT_VALUE_COLS = ["score", "bullish_pct", "bearish_pct", "buzz",
                         "articles_week", "sentiment_norm", "pe",
                         "claude_confidence", "avg_volume_10d", "avg_volume_3m",
                         "beta", "float_shares"]


def _analyze_sentiment(cfg: dict) -> dict:
    with get_conn() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT s.ticker, s.snapshot_ts, s.score, s.bullish_pct, s.bearish_pct, "
            "       s.buzz, s.articles_week, s.sentiment_norm, s.market_cap, s.pe, "
            "       s.pinned, s.claude_confidence, s.avg_volume_10d, s.avg_volume_3m, "
            "       s.beta, s.float_shares, s.sector, sfr.ret_pct "
            "FROM scan_snapshots s JOIN scan_forward_returns sfr ON sfr.snapshot_id = s.id "
            "WHERE sfr.horizon_days = ? AND sfr.ret_pct IS NOT NULL",
            (HORIZON,),
        ).fetchall()]

    n = len(rows)
    if n < MIN_SAMPLE:
        return _insufficient_report("sentiment", n)

    pos_thr, neg_thr = _thresholds(cfg, "sentiment")
    groups = _split_groups(rows, pos_thr, neg_thr)

    def _summarize(group_rows: list[dict], direction: str) -> dict:
        sectors = Counter(r["sector"] for r in group_rows if r.get("sector"))
        n_g = len(group_rows)
        return {
            "n": n_g,
            "avg_ret_pct": _mean([r["ret_pct"] for r in group_rows]),
            "stats": _group_stats(group_rows, _SENTIMENT_VALUE_COLS),
            "pinned_pct": round(sum(1 for r in group_rows if r["pinned"]) / n_g * 100, 1)
                          if n_g else None,
            "sector_distribution_pct": {s: round(c / n_g * 100, 1) for s, c in sectors.items()} if n_g else {},
            "examples": _top_examples(group_rows, "ret_pct", 5, reverse=(direction == "pos")),
        }

    return {
        "system": "sentiment",
        "sample_size": n,
        "insufficient_data": False,
        "grouping_method": groups["method"],
        "thresholds": {"pos": pos_thr, "neg": neg_thr},
        "period_start": min(r["snapshot_ts"] for r in rows)[:10],
        "period_end": max(r["snapshot_ts"] for r in rows)[:10],
        "pos_group": _summarize(groups["pos"], "pos"),
        "neg_group": _summarize(groups["neg"], "neg"),
    }


# ── Frühsignal-Analyse ─────────────────────────────────────────────────────────

def _analyze_early_signals(cfg: dict) -> dict:
    with get_conn() as conn:
        alert_rows = [dict(r) for r in conn.execute(
            "SELECT a.id, a.ticker, a.alert_ts, a.total_score, a.kind, a.signal_ids, fr.ret_pct "
            "FROM alerts a JOIN forward_returns fr ON fr.alert_id = a.id "
            "WHERE fr.horizon_days = ? AND fr.ret_pct IS NOT NULL",
            (HORIZON,),
        ).fetchall()]

        all_sig_ids = set()
        for a in alert_rows:
            try:
                all_sig_ids.update(json.loads(a["signal_ids"] or "[]"))
            except Exception:
                pass

        sig_map = {}
        if all_sig_ids:
            placeholders = ",".join("?" * len(all_sig_ids))
            for r in conn.execute(
                f"SELECT id, signal_type, details_json FROM signals WHERE id IN ({placeholders})",
                tuple(all_sig_ids),
            ).fetchall():
                try:
                    details = json.loads(r["details_json"] or "{}")
                except Exception:
                    details = {}
                sig_map[r["id"]] = {"signal_type": r["signal_type"], "details": details}

    n = len(alert_rows)
    if n < MIN_SAMPLE:
        return _insufficient_report("early_signals", n)

    for a in alert_rows:
        try:
            sig_ids = json.loads(a["signal_ids"] or "[]")
        except Exception:
            sig_ids = []
        a["_signals"] = [sig_map[sid] for sid in sig_ids if sid in sig_map]

    pos_thr, neg_thr = _thresholds(cfg, "early_signals")
    groups = _split_groups(alert_rows, pos_thr, neg_thr)

    def _summarize(group_rows: list[dict], direction: str) -> dict:
        n_g = len(group_rows)
        kind_counts = Counter(r["kind"] for r in group_rows)
        type_presence = Counter()
        insider_usd, insider_cluster_n, insider_n = [], 0, 0
        volume_z, buzz_accel, holder_pct = [], [], []
        for r in group_rows:
            types_here = set()
            for sig in r["_signals"]:
                t, d = sig["signal_type"], sig["details"]
                types_here.add(t)
                if t == "insider_buy":
                    insider_n += 1
                    if d.get("total_usd") is not None:
                        insider_usd.append(d["total_usd"])
                    if d.get("cluster"):
                        insider_cluster_n += 1
                elif t == "volume_anomaly" and d.get("z_score") is not None:
                    volume_z.append(d["z_score"])
                elif t == "buzz_accel" and d.get("rel_accel") is not None:
                    buzz_accel.append(d["rel_accel"])
                elif t == "large_holder" and d.get("pct") is not None:
                    holder_pct.append(d["pct"])
            type_presence.update(types_here)

        return {
            "n": n_g,
            "avg_ret_pct": _mean([r["ret_pct"] for r in group_rows]),
            "avg_total_score": _mean([r["total_score"] for r in group_rows]),
            "kind_distribution": dict(kind_counts),
            "signal_type_presence_pct": {
                t: round(c / n_g * 100, 1) for t, c in type_presence.items()
            } if n_g else {},
            "insider_buy_total_usd": {"mean": _mean(insider_usd), "median": _median(insider_usd)},
            "insider_cluster_pct": round(insider_cluster_n / insider_n * 100, 1) if insider_n else None,
            "volume_z_score": {"mean": _mean(volume_z), "median": _median(volume_z)},
            "buzz_rel_accel": {"mean": _mean(buzz_accel), "median": _median(buzz_accel)},
            "large_holder_pct": {"mean": _mean(holder_pct), "median": _median(holder_pct)},
            "examples": _top_examples(group_rows, "ret_pct", 5, reverse=(direction == "pos")),
        }

    return {
        "system": "early_signals",
        "sample_size": n,
        "insufficient_data": False,
        "grouping_method": groups["method"],
        "thresholds": {"pos": pos_thr, "neg": neg_thr},
        "period_start": min(r["alert_ts"] for r in alert_rows)[:10],
        "period_end": max(r["alert_ts"] for r in alert_rows)[:10],
        "pos_group": _summarize(groups["pos"], "pos"),
        "neg_group": _summarize(groups["neg"], "neg"),
    }


# ── Cross-System-Analyse (Ticker in beiden Systemen gleichzeitig) ─────────────
# Prüft, ob ein Sentiment-Scan-Snapshot, dessen Ticker auch innerhalb von ±7
# Tagen einen Frühsignal-Alert hatte, im Schnitt besser/schlechter performt als
# einer ohne diese Überschneidung – rein SQL, 0 Kosten. Josef-Wunsch 2026-08-06.

_CROSS_OVERLAP_DAYS = 7
_CROSS_MIN_OVERLAP = 3  # eigene Mindestgröße für die Overlap-Gruppe (kann bei
                        # insgesamt genug Daten trotzdem selten sein)


def _analyze_cross_signal(cfg: dict) -> dict:
    with get_conn() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT s.ticker, s.snapshot_ts, sfr.ret_pct, "
            "  EXISTS(SELECT 1 FROM alerts a WHERE a.ticker = s.ticker "
            "         AND ABS(julianday(a.alert_ts) - julianday(s.snapshot_ts)) <= ?) AS has_overlap "
            "FROM scan_snapshots s JOIN scan_forward_returns sfr ON sfr.snapshot_id = s.id "
            "WHERE sfr.horizon_days = ? AND sfr.ret_pct IS NOT NULL",
            (_CROSS_OVERLAP_DAYS, HORIZON),
        ).fetchall()]

    n = len(rows)
    overlap = [r for r in rows if r["has_overlap"]]
    no_overlap = [r for r in rows if not r["has_overlap"]]

    if n < MIN_SAMPLE or len(overlap) < _CROSS_MIN_OVERLAP:
        # ETA aus derselben Reifungs-Rate wie das Sentiment-System (gleiche
        # zugrundeliegende Tabelle scan_forward_returns)
        return {
            "system": "cross_signal", "sample_size": n, "insufficient_data": True,
            "min_sample": MIN_SAMPLE, "overlap_count": len(overlap),
            "min_overlap": _CROSS_MIN_OVERLAP, "eta_weeks": _eta_weeks("sentiment", n),
        }

    def _summarize(group_rows: list[dict]) -> dict:
        return {
            "n": len(group_rows),
            "avg_ret_pct": _mean([r["ret_pct"] for r in group_rows]),
            "examples": _top_examples(group_rows, "ret_pct", 5, reverse=True),
        }

    return {
        "system": "cross_signal",
        "sample_size": n,
        "insufficient_data": False,
        "overlap_days": _CROSS_OVERLAP_DAYS,
        "period_start": min(r["snapshot_ts"] for r in rows)[:10],
        "period_end": max(r["snapshot_ts"] for r in rows)[:10],
        "overlap_group": _summarize(overlap),
        "no_overlap_group": _summarize(no_overlap),
    }


# ── Speichern + Einstiegspunkte ────────────────────────────────────────────────

def analyze_and_store(cfg: dict, system: str) -> dict:
    if system == "sentiment":
        report = _analyze_sentiment(cfg)
    elif system == "early_signals":
        report = _analyze_early_signals(cfg)
    elif system == "cross_signal":
        report = _analyze_cross_signal(cfg)
    else:
        raise ValueError(f"Unbekanntes System: {system!r}")

    # _suggest_adjustments erwartet pos_group/neg_group – gibt es nur bei
    # sentiment/early_signals, cross_signal hat overlap_group/no_overlap_group
    if not report.get("insufficient_data") and system in ("sentiment", "early_signals"):
        report["suggestions"] = _suggest_adjustments(cfg, system, report)

    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    today = date.today().isoformat()
    period_start = report.get("period_start", today)
    period_end = report.get("period_end", today)
    insufficient = bool(report.get("insufficient_data"))

    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO weekly_reports "
            "(report_ts, system, period_start, period_end, sample_size, "
            " insufficient_data, report_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (now_iso, system, period_start, period_end, report.get("sample_size", 0),
             1 if insufficient else 0, json.dumps(report, ensure_ascii=False)),
        )
        report["id"] = cur.lastrowid

    report["report_ts"] = now_iso
    return report


def get_latest_report(system: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            # id statt report_ts (Sekundenpräzision) sortieren – zwei Analysen
            # innerhalb derselben Sekunde (z.B. manueller Re-Run) wären sonst
            # mehrdeutig geordnet
            "SELECT * FROM weekly_reports WHERE system=? ORDER BY id DESC LIMIT 1",
            (system,),
        ).fetchone()
    if not row:
        return None
    d = dict(row)
    d["report_json"] = json.loads(d["report_json"])
    return d


def run_weekly_analysis(cfg: dict) -> None:
    """Scheduler-Einstiegspunkt: analysiert alle drei Dimensionen, verschickt
    eine Telegram-Nachricht mit allen Abschnitten. 0 Kosten (kein Claude-Call)."""
    from scanner import _tg_post

    reports = {}
    for system in ("sentiment", "early_signals", "cross_signal"):
        try:
            reports[system] = analyze_and_store(cfg, system)
        except Exception:
            log.exception("Wöchentliche Analyse fehlgeschlagen (%s)", system)

    lines = ["📈 <b>Wöchentliche Performance-Analyse</b>", ""]
    for system, label in (("sentiment", "Sentiment-Scan"), ("early_signals", "Frühsignale"),
                          ("cross_signal", "Cross-Signal (beide Systeme)")):
        r = reports.get(system)
        lines.append(f"<b>{label}</b>")
        if not r:
            lines.append("Fehler bei der Analyse.")
        elif r.get("insufficient_data"):
            eta = r.get("eta_weeks")
            eta_txt = f", noch ~{eta} Wochen" if eta else ""
            if system == "cross_signal":
                lines.append(f"Noch nicht genug Überschneidungen ({r.get('overlap_count', 0)}/"
                             f"{r.get('min_overlap', _CROSS_MIN_OVERLAP)}{eta_txt}).")
            else:
                lines.append(f"Noch nicht genug Daten ({r['sample_size']}/{MIN_SAMPLE}{eta_txt}).")
        elif system == "cross_signal":
            ov, no_ov = r["overlap_group"], r["no_overlap_group"]
            lines.append(
                f"Überschneidung (n={ov['n']}, Ø {ov['avg_ret_pct']:+.1f}%) vs. "
                f"kein Überlapp (n={no_ov['n']}, Ø {no_ov['avg_ret_pct']:+.1f}%)"
            )
        else:
            pos, neg = r["pos_group"], r["neg_group"]
            lines.append(
                f"Positiv (n={pos['n']}, Ø {pos['avg_ret_pct']:+.1f}%) vs. "
                f"Negativ (n={neg['n']}, Ø {neg['avg_ret_pct']:+.1f}%)"
            )
            if system == "sentiment":
                bp, bn = pos["stats"]["bullish_pct"]["mean"], neg["stats"]["bullish_pct"]["mean"]
                zp, zn = pos["stats"]["buzz"]["mean"], neg["stats"]["buzz"]["mean"]
                lines.append(f"Bullish% Ø {bp} vs {bn} · Buzz Ø {zp} vs {zn}")
            else:
                lines.append(
                    f"Insider-Cluster: {pos.get('insider_cluster_pct')}% vs "
                    f"{neg.get('insider_cluster_pct')}%"
                )
            top_sugg = (r.get("suggestions") or [None])[0]
            if top_sugg:
                flag = "⚠️ " if top_sugg["kind"] == "unexpected_inverse" else "💡 "
                lines.append(f"{flag}{top_sugg['label']}: {top_sugg['rel_diff_pct']:+.0f}% Unterschied "
                             f"(Konfidenz: {top_sugg['confidence']})")
        lines.append("")
    lines.append("Details im Analyse-Tab der App.")
    _tg_post("\n".join(lines))


def generate_ai_text(report_row: dict, system: str) -> dict:
    """KI-Interpretation eines bereits berechneten Reports (report_row von
    get_latest_report()). Kein neuer Datendurchlauf, nur ein Claude-Call über
    den bereits vorliegenden report_json – löst reale Kosten aus."""
    if not os.environ.get("CLAUDE_API_KEY"):
        raise ValueError("Kein Claude-API-Key konfiguriert")

    today_cost = costs.load_costs_summary()["today"].get("cost_usd", 0.0)
    if today_cost >= costs.DAILY_HARD_KILL_USD:
        raise RuntimeError(f"Tages-Kostenlimit bereits erreicht (${today_cost:.2f})")

    report = report_row["report_json"]
    if isinstance(report, str):
        report = json.loads(report)
    if report.get("insufficient_data"):
        raise ValueError("Noch nicht genug Daten für eine KI-Interpretation")

    label = {"sentiment": "Sentiment-Scan", "early_signals": "Frühsignale",
             "cross_signal": "Cross-Signal (Überschneidung Sentiment-Scan/Frühsignale)"}[system]

    if system == "cross_signal":
        task_txt = (
            "Schreibe eine kurze, konkrete deutsche Zusammenfassung (max. 120 Wörter): "
            "Performt die Überschneidungs-Gruppe (overlap_group) klar anders als die "
            "Gruppe ohne Überlapp (no_overlap_group)? Wie belastbar ist das angesichts "
            "der Stichprobengröße? Nenne keine Anlageempfehlung, nur die beobachteten "
            "Muster in den Daten und deren Belastbarkeit."
        )
    else:
        task_txt = (
            "Im Feld 'suggestions' stehen bereits regelbasiert berechnete Auffälligkeiten "
            "(inkl. 'unexpected_inverse' = Fälle, in denen die bisherige Annahme 'höherer "
            "Wert = stärkeres Signal' den Daten widerspricht). Schreibe eine kurze, "
            "konkrete deutsche Zusammenfassung (max. 150 Wörter): Ordne diese Vorschläge "
            "fachlich ein – welche sind angesichts der Stichprobengröße plausibel, welche "
            "eher Zufall? Was unterscheidet die positive von der negativen Gruppe am "
            "deutlichsten? Nenne keine Anlageempfehlung, nur die beobachteten Muster in "
            "den Daten und deren Belastbarkeit."
        )

    prompt = (
        f"Hier ist eine regelbasierte statistische Auswertung ({label}) aus einem "
        f"Aktien-Signal-System (Kursrendite {HORIZON} Handelstage später).\n\n"
        f"{json.dumps(report, ensure_ascii=False, indent=2)}\n\n"
        f"{task_txt}"
    )

    client = anthropic.Anthropic(api_key=os.environ.get("CLAUDE_API_KEY", ""))
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=500,
        messages=[{"role": "user", "content": prompt}],
    )
    text = resp.content[0].text.strip()

    cost_result = costs.record_call(
        "claude-haiku-4-5-20251001", resp.usage.input_tokens, resp.usage.output_tokens,
        context=f"weekly_analysis_ai_{system}",
    )

    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with get_conn() as conn:
        conn.execute(
            "UPDATE weekly_reports SET ai_text=?, ai_generated_ts=? WHERE id=?",
            (text, now_iso, report_row["id"]),
        )

    return {"text": text, "cost_usd": cost_result["cost_usd"],
            "day_total_usd": cost_result["day_total_usd"]}
