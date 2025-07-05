#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
forecast_morgen.py – Fang-Prognose für *morgen* mit Live-Wetter aus PirateWeather
================================================================================

• Holt stündliche Vorhersage-Daten via PirateWeather-API.
• Fixiert die Systemzeit im Modul `sensor_berechnung` auf den kommenden Kalendertag,
  sodass alle nachgelagerten Berechnungen (Wassertemperatur, Fangwahrscheinlichkeit,
  Fangfenster, Tipps …) rein prognostisch laufen.
• Berechnet die Fangwahrscheinlichkeit mit **genau derselben Logik** wie das Live-Skript.
• Publisht die Ergebnisse optional sofort via MQTT, wenn `mqtt.py` verfügbar ist.

Benötigte Umgebungsvariablen
----------------------------
PIRATE_API_KEY  – API-Key von https://pirateweather.net
LATITUDE        – Breitengrad (z. B. 52.52)
LONGITUDE       – Längengrad (z. B. 13.40)
TZ              – Zeitzone, Default Europe/Berlin
(MQTT-Settings übernimmt `mqtt.py` wie gewohnt.)

Aufruf
------
    python forecast_morgen.py        # schreibt jedes Ergebnis als JSON + publisht via MQTT
    python forecast_morgen.py | jq . # nur Ausgabe, wenn du MQTT nicht brauchst
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta
from typing import Any, Dict, List
from zoneinfo import ZoneInfo

import requests

import sensor_berechnung as sb  # Projekt-Modul

# ───────────────────────────────────────────────────────────────────────────────
# Basis-Konstanten & Logging
# ───────────────────────────────────────────────────────────────────────────────
_TZ = ZoneInfo(os.getenv("TZ", "Europe/Berlin"))
logger = logging.getLogger("forecast_morgen")
logging.basicConfig(level=logging.INFO, format="%(levelname)-8s %(message)s")

# Morgen 00:00 Uhr (lokal)
_BASE_NOW = (
    datetime.now(_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    + timedelta(days=1)
)


class FrozenDateTime(datetime):
    """Ersetzt `datetime.now()` durch eine feste Zeit (_BASE_NOW)."""

    @classmethod
    def now(cls, tz: ZoneInfo | None = None):  # type: ignore[override]
        return _BASE_NOW if tz is None else _BASE_NOW.astimezone(tz)


# Sensor-Modul auf Morgen einfrieren
sb.datetime = FrozenDateTime  # type: ignore[attr-defined]

# ───────────────────────────────────────────────────────────────────────────────
# PirateWeather: Stunden für *morgen* laden
# ───────────────────────────────────────────────────────────────────────────────

def _fetch_pirateweather_data(lat: str, lon: str, key: str) -> List[Dict[str, Any]]:
    url = (
        f"https://api.pirateweather.net/forecast/{key}/{lat},{lon}"
        "?units=si&exclude=minutely,daily,alerts&extend=hourly"
    )
    logger.info("→ PirateWeather abrufen …")
    resp = requests.get(url, timeout=10)
    resp.raise_for_status()

    hourly = resp.json().get("hourly", {}).get("data", [])
    if not hourly:
        raise RuntimeError("PirateWeather lieferte keine Hourly-Daten")

    tomorrow = _BASE_NOW.date()
    day_after = tomorrow + timedelta(days=1)

    records: List[Dict[str, Any]] = []
    for h in hourly:
        ts = datetime.fromtimestamp(h["time"], tz=_TZ)
        if not tomorrow <= ts.date() < day_after:
            continue  # nur Stunden von morgen

        # für jede Fischart ein Eintrag (wie BasicSensor das im Live-Betrieb macht)
        for art in sb.load_preferences().keys():
            records.append({
                "Art": art,
                "timestamp": ts.isoformat(),
                "Oberfläche_temp": h.get("temperature"),
                "Luftdruck":      h.get("pressure"),
                "Windgeschwindigkeit": h.get("windSpeed"),
                "windBearing":   h.get("windBearing"),
                "cloudFraction": h.get("cloudCover"),
                "precipIntensity": h.get("precipIntensity"),
            })

    logger.info("→ %s Stunden × %s Fisch(e) → %s Datensätze geladen",
                len(set(r["timestamp"] for r in records)), len(sb.load_preferences()), len(records))
    return records


# ───────────────────────────────────────────────────────────────────────────────
# Forecast für morgen berechnen (wie Live-Modus)
# ───────────────────────────────────────────────────────────────────────────────

def forecast_for_tomorrow() -> List[Dict[str, Any]]:
    lat = os.getenv("LATITUDE")
    lon = os.getenv("LONGITUDE")
    api_key = os.getenv("PIRATE_API_KEY") or os.getenv("PIRATEWEATHER_API_KEY")

    if lat and lon and api_key:
        try:
            raw = _fetch_pirateweather_data(lat, lon, api_key)
        except Exception as exc:  # noqa: BLE001
            logger.error("PirateWeather fehlgeschlagen: %s – nutze lokale Daten", exc)
            raw = sb.load_sensor_data()
    else:
        logger.warning("LAT/LON/API-Key fehlen – nutze lokale Sensordaten")
        raw = sb.load_sensor_data()

    prefs   = sb.load_preferences()
    cleaned = sb.clean_and_calculate(raw)

    try:
        sunrise, sunset = sb.get_sun_times(lat, lon, _TZ.key)
    except Exception as exc:  # noqa: BLE001
        logger.warning("get_sun_times() fehlgeschlagen: %s – Fallback 05/21 Uhr", exc)
        sunrise = _BASE_NOW.replace(hour=5)
        sunset  = _BASE_NOW.replace(hour=21)

    return sb.compute_catch_probability_and_window(
        cleaned,
        prefs,
        {"sunrise": sunrise, "sunset": sunset},
    )


# ───────────────────────────────────────────────────────────────────────────────
# Optional: direkt per MQTT publizieren, wenn mqtt.py vorhanden ist
# ───────────────────────────────────────────────────────────────────────────────
try:
    from mqtt import publish_sensor_data  # type: ignore
except ImportError:  # Fallback: No MQTT available
    def publish_sensor_data(_):
        pass


# ───────────────────────────────────────────────────────────────────────────────
# CLI / Entrypoint
# ───────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    recs = forecast_for_tomorrow()
    for rec in recs:
        print(json.dumps(rec, ensure_ascii=False))
        publish_sensor_data(rec)
