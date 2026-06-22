#!/usr/bin/env python3
"""Einmaliges Script: US-Aktien (NYSE + NASDAQ) von Finnhub laden.
Aufruf: python3 fetch_tickers.py
Erzeugt: tickers.csv (Spalten: ticker, name)
Hinweis: Breiter als Russell 2000, aber MarketCap-Filter im Scanner grenzt auf Small Caps ein.
Quartalsweise wiederholen um neue/delisted Ticker zu aktualisieren.
"""
import csv
import os
import sys
import requests
from pathlib import Path

OUT = Path(__file__).parent / "tickers.csv"
_SECRETS = Path("/etc/pka/secrets.env")

VALID_MIC = {"XNYS", "XNAS"}  # NYSE + NASDAQ


def _load_env():
    if os.environ.get("FINNHUB_API_KEY"):
        return
    if not _SECRETS.exists():
        return
    for line in _SECRETS.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def main():
    _load_env()
    key = os.environ.get("FINNHUB_API_KEY", "")
    if not key:
        print("FEHLER: FINNHUB_API_KEY nicht gesetzt.")
        sys.exit(1)

    print("Lade US-Aktien (NYSE + NASDAQ) von Finnhub …")
    r = requests.get(
        "https://finnhub.io/api/v1/stock/symbol",
        params={"exchange": "US", "token": key},
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()

    symbols = [
        d for d in data
        if d.get("mic") in VALID_MIC and d.get("type") == "Common Stock"
    ]
    print(f"Gefiltert (Common Stock, NYSE+NASDAQ): {len(symbols)} Ticker")

    count = 0
    with open(OUT, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["ticker", "name"])
        for s in symbols:
            ticker = s.get("symbol", "").strip()
            name = s.get("description", "").strip()
            if ticker:
                writer.writerow([ticker, name])
                count += 1

    print(f"Fertig: {count} Ticker in {OUT}")


if __name__ == "__main__":
    main()
