#!/usr/bin/env python3
"""Einmaliges Script: Russell 2000 Ticker von iShares laden.
Aufruf: python3 fetch_tickers.py
Erzeugt: tickers.csv (Spalten: ticker, name)
"""
import csv
import io
import sys
import requests
from pathlib import Path

# iShares Russell 2000 ETF (IWM) – CSV-Download
URL = (
    "https://www.ishares.com/us/products/239710/IWM/"
    "1467271812596.ajax?fileType=csv&fileName=IWM_holdings&dataType=fund"
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.ishares.com/us/products/239710/",
}

OUT = Path(__file__).parent / "tickers.csv"


def main():
    print("Lade Russell 2000 Holdings von iShares …")
    resp = requests.get(URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()

    lines = resp.text.splitlines()

    # Die iShares-CSV hat Metadaten-Zeilen am Anfang.
    # Die echte Kopfzeile enthält "Ticker" als erstes Feld.
    start = None
    for i, line in enumerate(lines):
        if line.startswith("Ticker,") or line.startswith('"Ticker"'):
            start = i
            break

    if start is None:
        print("FEHLER: Kopfzeile nicht gefunden. iShares hat evtl. das Format geändert.")
        print("Ersten 10 Zeilen zur Diagnose:")
        for l in lines[:10]:
            print(" ", l)
        sys.exit(1)

    reader = csv.DictReader(io.StringIO("\n".join(lines[start:])))

    count = 0
    with open(OUT, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["ticker", "name"])
        for row in reader:
            ticker = (row.get("Ticker") or "").strip()
            name = (row.get("Name") or "").strip()
            # Überspringe Cash-Positionen und leere Zeilen
            if ticker and ticker not in ("-", "USD", "") and not ticker.startswith("-"):
                writer.writerow([ticker, name])
                count += 1

    print(f"Fertig: {count} Ticker in {OUT}")


if __name__ == "__main__":
    main()
