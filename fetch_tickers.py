#!/usr/bin/env python3
"""Einmaliges Script: Russell 2000 Ticker via Finnhub-API laden.
Aufruf: python3 fetch_tickers.py
Erzeugt: tickers.csv (Spalten: ticker, name)
Voraussetzung: FINNHUB_API_KEY in Umgebungsvariablen gesetzt.
"""
import csv
import os
import sys
import requests
from pathlib import Path

KEY = os.environ.get("FINNHUB_API_KEY", "")
OUT = Path(__file__).parent / "tickers.csv"


def main():
    if not KEY:
        print("FEHLER: FINNHUB_API_KEY nicht gesetzt.")
        sys.exit(1)

    print("Lade Russell 2000 Constituents von Finnhub (/indices/constituents?symbol=^RUT) …")
    r = requests.get(
        "https://finnhub.io/api/v1/indices/constituents",
        params={"symbol": "^RUT", "token": KEY},
        timeout=30,
    )

    if r.status_code == 403:
        print("FEHLER: Finnhub liefert 403 – Indices-Endpoint ist im Free Tier nicht verfügbar.")
        print("→ Manuelle Alternative: tickers.csv lokal vorbereiten und per scp hochladen.")
        print("  Quelle: https://www.ishares.com/us/products/239710/ → 'Download Holdings' (CSV)")
        print("  Dann: scp tickers.csv root@89.167.104.145:/opt/sentiment-scanner/tickers.csv")
        sys.exit(1)

    r.raise_for_status()
    data = r.json()
    constituents = data.get("constituents", [])

    if not constituents:
        print(f"FEHLER: Keine Daten erhalten. Antwort: {data}")
        sys.exit(1)

    count = 0
    with open(OUT, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["ticker", "name"])
        for item in constituents:
            if isinstance(item, str):
                # Finnhub gibt manchmal nur Ticker-Strings zurück
                writer.writerow([item.strip(), ""])
            elif isinstance(item, dict):
                ticker = item.get("symbol", "").strip()
                name = item.get("description", "").strip()
                if ticker:
                    writer.writerow([ticker, name])
            else:
                continue
            count += 1

    print(f"Fertig: {count} Ticker in {OUT}")


if __name__ == "__main__":
    main()
