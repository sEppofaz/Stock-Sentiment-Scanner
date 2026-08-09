"""Gemeinsamer yfinance-Zugriff für forward_tracker.py und scan_tracker.py."""
import logging

import yfinance as yf

log = logging.getLogger("scanner")


def fetch_closes(ticker: str, start_date: str):
    """Tages-Close-Reihe ab start_date (YYYY-MM-DD), Index 0 = erster Handelstag
    ab start_date. Gibt None bei Fehler ODER wenn yfinance keine Daten liefert
    (z.B. delisteter Ticker) zurück (bereits geloggt) – Aufrufer verlassen sich
    beide auf "None = kein verwertbares Ergebnis" statt selbst auf Leere zu
    prüfen (live gefunden 2026-08-09: scan_tracker.py crashte mit IndexError
    auf closes.iloc[0], da eine leere Series kein Fehler im try/except war).

    yfinance liefert auch bei einzelnem String-Ticker MultiIndex-Spalten
    (verifiziert 2026-07-06) -> hist["Close"] ist ein DataFrame, kein Series,
    daher hist["Close"][ticker] nötig, sonst crasht float(...)."""
    try:
        hist = yf.download(ticker, start=start_date, interval="1d",
                           progress=False, auto_adjust=False)
        closes = hist["Close"][ticker].dropna()
        if closes.empty:
            log.warning("yf_helper %s: keine Kursdaten ab %s (delistet?)", ticker, start_date)
            return None
        return closes
    except Exception as e:
        log.warning("yf_helper %s: %s", ticker, e)
        return None
