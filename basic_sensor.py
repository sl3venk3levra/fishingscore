# -*- coding: utf-8 -*-
"""
Sammelt Rohdaten pro Fischart und leitet sie unverändert an
`sensor_berechnung.py` weiter.

Datenquellen
────────────
1. **fisch.json** – statische Präferenzen je Fischart
2. **PirateWeather-API** – Temperatur, Luftdruck, Wind, Mondphase, Sonnenzeiten
3. **GKD-Webseite** – aktuelle Oberflächentemperatur Chiemsee / Stock
4. **BayAVFiG-Anlage 1** – Schonzeiten & Schonmaße (HTML-Tabelle)

• Alle Secrets / URLs stehen in der `.env`.
• HTML-Parsing erfolgt mit *BeautifulSoup* →
"""
from __future__ import annotations
import json
import os
import re
import logging
import ephem
from zoneinfo import ZoneInfo
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional

import requests
from bs4 import BeautifulSoup  # pip install beautifulsoup4 lxml
from dotenv import load_dotenv
from logging_config import setup_logging

# ─────────────────────────── Environment und Logging
load_dotenv()
_TZ = ZoneInfo(os.getenv("TZ", "Europe/Berlin"))

# Force DEBUG-Level auf Root-Logger
logging.getLogger().setLevel(logging.DEBUG)
setup_logging()
log = logging.getLogger(__name__)

MONTHS_DE = {
    "Januar": 1, "Februar": 2, "März": 3, "April": 4,
    "Mai": 5, "Juni": 6, "Juli": 7, "August": 8,
    "September": 9, "Oktober": 10, "November": 11, "Dezember": 12,
}


def _env(key: str) -> str:
    val = os.getenv(key)
    if not val:
        raise RuntimeError(f"Environment variable '{key}' missing")
    return val


class BasicSensor:
    """
    Erzeugt Sensor-Objekte und füllt das Attribut *raw* mit Live-Daten.
    """

    @staticmethod
    def _parse_date_span(span: str) -> tuple[datetime.date, datetime.date] | None:
        """
        Sucht nach zwei Datumsangaben im Format 'D. Monat' getrennt durch 'bis' oder Striche.
        Gibt (start, end) als date zurück oder None, wenn nichts gefunden.
        """
        # Alle möglichen Trenner: bis, -, –, —
        parts = re.split(r"\s*(?:bis|[-–—])\s*", span)
        if len(parts) != 2:
            return None
        def to_date(txt: str) -> datetime.date:
            txt = txt.strip()
            # „1. Mai“ → Tag und Monat
            match = re.match(r"(\d{1,2})\.\s*([A-Za-zäöüÄÖÜ]+)", txt)
            if not match:
                raise ValueError(f"Ungültiges Datum: {txt}")
            day, month_name = match.groups()
            month = MONTHS_DE.get(month_name)
            if not month:
                raise ValueError(f"Unbekannter Monat: {month_name}")
            today = datetime.now(tz=_TZ).date()
            # Jahreslogik: wenn Start im Dez und heute im Jan, Jahreswechsel berücksichtigen?
            return datetime(today.year, month, int(day), tzinfo=_TZ).date()
        try:
            start, end = to_date(parts[0]), to_date(parts[1])
            return start, end
        except Exception as e:
            log.warning("Schonzeit-Parsing fehlgeschlagen für '%s': %s", span, e)
            return None


    def __init__(self, art: str, prefs: Dict[str, Any]):
        self.art = art
        self.prefs = prefs
        self.raw: Dict[str, Any] = {}

    @staticmethod
    def _fetch_moon_phase() -> str:
        now = datetime.now(tz=_TZ)
        phase = ephem.Moon(now).phase
        if phase <= 1:
            return "Neumond"
        if phase >= 99:
            return "Vollmond"
        if 40 <= phase <= 60:
            return "Halbmond"
        tomorrow = ephem.Moon(now + timedelta(days=1)).phase
        return "Zunehmender Mond" if tomorrow > phase else "Abnehmender Mond"

    @staticmethod
    def _fetch_weather_json() -> Dict[str, Any]:
        key = _env("PIRATEWEATHER_API_KEY")
        lat, lon = _env("LATITUDE"), _env("LONGITUDE")
        url = (f"https://api.pirateweather.net/forecast/{key}/{lat},{lon}?"
               "units=si&exclude=minutely,alerts,flags")
        try:
            r = requests.get(url, timeout=10)
            r.raise_for_status()
            data = r.json()
            log.debug("Full weather JSON: %s", data)
            return data
        except Exception as e:
            log.error("PirateWeather-Fehler: %s", e)
            return {}

    @staticmethod
    def _fetch_surface_temp() -> Optional[float]:
        url = _env("SEA_TEMP_URL")
        try:
            r = requests.get(url, timeout=10)
            r.raise_for_status()
            soup = BeautifulSoup(r.text, "lxml")
            td = soup.select_one("table tr:nth-of-type(2) td:nth-of-type(2)")
            return float(td.text.strip().replace(",", ".")) if td else None
        except Exception as e:
            log.error("Sea-Temp-Fehler: %s", e)
            return None

    @staticmethod
    def _active_in_year(span: str) -> bool:
        if "bis" not in span:
            return False
        try:
            start_s, end_s = [s.strip() for s in span.split("bis")]
            today = datetime.now(tz=_TZ).date()
            year = today.year
            def parse(s: str) -> datetime.date:
                d, m = s.split(". ")
                return datetime(year, MONTHS_DE[m], int(d)).date()
            start, end = parse(start_s), parse(end_s)
            return start <= today <= end if start <= end else today >= start or today <= end
        except Exception as e:
            log.warning("Schonzeit-Parsing fehlgeschlagen: %s", e)
            return False

    @classmethod
    def _fetch_schonzeit_mass(cls, art: str) -> Dict[str, Any]:
        url = _env("FANGZEITEN_URL")
        try:
            soup = BeautifulSoup(requests.get(url, timeout=10).text, "lxml")
        except Exception as e:
            log.error("Schonzeit-URL-Fehler: %s", e)
            return {"Schonzeit": None, "Schonmaß_cm": None, "Schonzeit_aktiv": None, "Schonbereich": None}

        for row in soup.select("table tr"):
            cells = [c.get_text(strip=True) for c in row.find_all(["td","th"])]
            if len(cells) < 4:
                continue
            key = re.sub(r"\s+", " ", cells[1]).lower()
            if art.lower() not in key:
                continue

            span_text = cells[2]
            datum_span = cls._parse_date_span(span_text) if span_text and span_text != "–" else None
            sz = span_text if datum_span else None

            cm_text = cells[3]
            if cm_text and cm_text != "–":
                # Falls "20–30 cm" oder "ab 20 cm" → wir nehmen den minimalen Wert
                nums = re.findall(r"\d+", cm_text)
                cm = float(nums[0]) if nums else None
            else:
                cm = None

            # Region dynamisch aus letztem Feld
            region_cell = cells[-1]
            region = "D" if "D" in region_cell else "EU"

            active = None
            if datum_span:
                start, end = datum_span
                today = datetime.now(tz=_TZ).date()
                # Wenn Zeitraum über Jahreswechsel geht
                active = (start <= today <= end) if start <= end else (today >= start or today <= end)

            return {
                "Schonzeit": sz,
                "Schonmaß_cm": cm,
                "Schonzeit_aktiv": active,
                "Schonbereich": region
            }

        log.warning("Schonzeit für %s nicht gefunden", art)
        return {"Schonzeit": None, "Schonmaß_cm": None, "Schonzeit_aktiv": None, "Schonbereich": None}

    @staticmethod
    def _compute_fangfenster(daily: Dict[str, Any]) -> str:
        try:
            sr = datetime.fromtimestamp(daily["sunriseTime"], tz=_TZ)
            ss = datetime.fromtimestamp(daily["sunsetTime"], tz=_TZ)
            morning = f"{sr:%H:%M}-{(sr+timedelta(hours=2)):%H:%M}"
            evening = f"{(ss-timedelta(hours=2)):%H:%M}-{ss:%H:%M}"
            return f"{morning} / {evening}"
        except:
            return ""

    @classmethod
    def create_all(cls) -> List[BasicSensor]:
        with open(os.path.join(os.path.dirname(__file__), "fisch.json"), encoding="utf-8") as f:
            prefs_list = json.load(f)
        data = cls._fetch_weather_json()
        current = data.get("currently", {})
        hourly = data.get("hourly", {}).get("data", [])
        daily = data.get("daily", {}).get("data", [])

        log.debug("current block: %s", current)
        log.debug("hourly timestamps: %s", [h.get("time") for h in hourly])

        sensors: List[BasicSensor] = []
        for entry in prefs_list:
            s = cls(entry["Art"], {k:v for k,v in entry.items() if k!="Art"})
            # Basisdaten
            s.raw.update({
                "Temperatur jetzt": current.get("temperature"),
                "Luftdruck": current.get("pressure"),
                "Windgeschwindigkeit": current.get("windSpeed"),
                "windBearing":     current.get("windBearing")
            })
            s.raw["Mondphase"] = cls._fetch_moon_phase()
            # Fangfenster
            s.raw["Bestes_Fangfenster"] = cls._compute_fangfenster(daily[0] if daily else {})
            # Oberfläche & Schonzeit
            s.raw["Oberfläche_temp"] = cls._fetch_surface_temp()
            s.raw.update(cls._fetch_schonzeit_mass(s.art))
            # Saison
            s.raw["saison"] = ("Winter","Frühling","Sommer","Herbst")[datetime.now(tz=_TZ).month%12//3]

            # Niederschlag
            precip = current.get("precipIntensity", 0.0)
            log.debug("precipIntensity: %s mm/h", precip)
            s.raw["precipIntensity"] = precip

            # Glättung cloudCover
            cc_now = float(current.get("cloudCover", 0.0))
            log.debug("cloudCover now: %s%%", round(cc_now*100,1))
            log.debug("Entering smoothing block, count=%d", len(hourly))
            if hourly:
                ts_now = int(datetime.now(tz=_TZ).timestamp())
                idx = min(range(len(hourly)), key=lambda i: abs(hourly[i]["time"]-ts_now))
                prev_i = max(0, idx-1)
                next_i = min(len(hourly)-1, idx)
                prev_cc = float(hourly[prev_i].get("cloudCover", cc_now))
                next_cc = float(hourly[next_i].get("cloudCover", cc_now))
                avg_cc = (prev_cc + next_cc) / 2
                log.debug("cloudCover smoothing: prev=%s%% next=%s%% avg=%s%%", round(prev_cc*100,1), round(next_cc*100,1), round(avg_cc*100,1))
                s.raw["cloudFraction"] = avg_cc
            else:
                s.raw["cloudFraction"] = cc_now

            # letzte Aktualisierung
            s.raw["letzte_aktualisierung"] = datetime.now(tz=_TZ).isoformat()

            sensors.append(s)
        return sensors

    @classmethod
    def get_consolidated_sensor_data(cls) -> List[Dict[str, Any]]:
        return [{**{"Art": s.art}, **s.prefs, **s.raw} for s in cls.create_all()]
