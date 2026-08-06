"""Gemeinsamer yfinance-Zugriff für forward_tracker.py und scan_tracker.py."""
import logging

import yfinance as yf

log = logging.getLogger("scanner")


def fetch_closes(ticker: str, start_date: str):
    """Tages-Close-Reihe ab start_date (YYYY-MM-DD), Index 0 = erster Handelstag
    ab start_date. Gibt None bei Fehler zurück (bereits geloggt).

    yfinance liefert auch bei einzelnem String-Ticker MultiIndex-Spalten
    (verifiziert 2026-07-06) -> hist["Close"] ist ein DataFrame, kein Series,
    daher hist["Close"][ticker] nötig, sonst crasht float(...)."""
    try:
        hist = yf.download(ticker, start=start_date, interval="1d",
                           progress=False, auto_adjust=False)
        return hist["Close"][ticker].dropna()
    except Exception as e:
        log.warning("yf_helper %s: %s", ticker, e)
        return None
