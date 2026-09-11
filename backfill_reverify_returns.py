"""Einmal-Skript (2026-09-11): prüft die komplette forward_returns/
scan_forward_returns-Historie gegen einen frischen yfinance-Fetch nach und
korrigiert Yahoo-Datenkorrekturen, die beim ursprünglichen Fill noch nicht
sichtbar waren (siehe return_reverify.py für den vollen Hintergrund).

Live gefunden: SXTC (+4.747,83% -> -39,4%), OMH (+2.879,17% -> -40,42%),
ONFO (+2.481,4% -> -48,37%), NXTT (+8.666,67% -> -12,3%) - alle vier waren
zum Zeitpunkt des ursprünglichen Trackers durch einen vorübergehend
fehlerhaften Yahoo-Close am Horizont-Tag verfälscht.

Nutzung (einmalig, auf dem Server im venv):
    venv/bin/python3 backfill_reverify_returns.py
"""
import logging

from return_reverify import reverify_all

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("backfill_reverify")


def main():
    n_fwd, n_scan = reverify_all()
    log.info("Fertig: %d forward_returns + %d scan_forward_returns korrigiert.", n_fwd, n_scan)


if __name__ == "__main__":
    main()
