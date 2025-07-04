from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
import math
import ephem
from zoneinfo import ZoneInfo
from logging_config import setup_logging
from basic_sensor import BasicSensor

# -----------------------------------------------------------------------
# Basiskonfiguration
# -----------------------------------------------------------------------
_TZ = ZoneInfo(os.getenv("TZ", "Europe/Berlin"))
setup_logging()
logger = logging.getLogger("sensor_berechnung")


# -----------------------------------------------------------------------
# Pfade zu Konfigurationsdateien
# -----------------------------------------------------------------------
PREF_PATH = "fisch.json"
PREV_PRESS_FILE = "pressure_prev.json"
WEIGHTS_PATH = "weights.json"

# -----------------------------------------------------------------------
# Gewichte und Puffer laden
# -----------------------------------------------------------------------
def load_weights() -> Dict[str, float]:
    try:
        with open(WEIGHTS_PATH, encoding="utf-8") as wf:
            cfg = json.load(wf)
        w = cfg.get("Bevorzugte_Gewichtungen", {})
        if not isinstance(w, dict):
            raise ValueError("Ungültige Gewichtungen")
        return w
    except Exception:
        logger.warning("Fehler beim Laden der Gewichtungen, verwende Fallback")
        return {
            "Saison":             12,
            "Temperatur":         10,
            "Wassertiefe":         7,
            "TempTiefe_Match":     13,
            "Tageszeitfenster":   14,
            "Nacht_Boost":         8,
            "Luftdruck_trend":     7,
            "Niederschlag":        8,
            "Bewölkung":           4,
            "Mondphase":           6,
            "Windrichtung":        3,
            "Windig":              4,
            "Trübung":             4
        }


def load_buffers() -> Dict[str, int]:
    try:
        with open(WEIGHTS_PATH, encoding="utf-8") as wf:
            cfg = json.load(wf)
        return cfg.get("Zeitfenster_Puffer", {})
    except Exception:
        logger.warning("Fehler beim Laden der Puffer, verwende Defaults")
        return {"Dämmerung": 45, "Tag": 30, "Nacht": 30}

WEIGHTS = load_weights()
BUFFERS = load_buffers()

TREND_MAP = {
    "leicht fallend":  "fallend",
    "leicht steigend": "steigend",
    "stabil":          "stagnierend",
    "gleichbleibend":  "stagnierend",
}
HALF_MOONS = {"Zunehmender Mond", "Abnehmender Mond"}

# -----------------------------------------------------------------------
# Hilfsfunktionen
# -----------------------------------------------------------------------
def round_to_next_five(pct: int) -> int:
    """
    Rundet immer auf das nächsthöhere Vielfache von 5,
    begrenzt den Wert sauber zwischen 0 und 100 %.
    """
    return max(0, min(100, int(math.ceil(pct / 5) * 5)))

def classify_clouds(fraction: Optional[float]) -> str:
    if fraction is None:
        return "unbekannt"
    if fraction >= 0.5:
        return "bewölkt"
    if fraction <= 0.2:
        return "klar"
    return "wechselhaft"

def classify_precip(mm_h: Optional[float]) -> str:
    if mm_h is None:
        return "unbekannt"
    return "regen" if mm_h >= 0.2 else "trocken"

def classify_trübung(entry: Dict[str, Any]) -> str:
    val = entry.get("Trübung")
    if val in ["trüb", "stark trüb", 2]:
        return "trüb"
    if val in ["leicht trüb", 1]:
        return "leicht trüb"
    return "klar"

def grad_to_windrichtung(grad: Optional[float]) -> str:
    if grad is None:
        return "unbekannt"
    dirs = ["N", "NO", "O", "SO", "S", "SW", "W", "NW"]
    ix = int((grad + 22.5) // 45) % 8
    return dirs[ix]

def time_in_window(now: datetime, s: datetime, e: datetime) -> bool:
    # Achtung: s und e können auf unterschiedlichen Tagen liegen (z. B. Nacht)
    if s < e:
        return s <= now <= e
    else:
        # Fenster geht über Mitternacht!
        return now >= s or now <= e
    

def temp_profile(depth: float, surface_temp: float, season: str) -> float:
    """
    Liefert Wassertemperatur [°C] in <depth> m.
    Vereinfachtes Schichtmodell für mitteleuropäische Seen.
    """
    # ─ Sommer: stabile Schichtung ─────────────────────────────
    if season == "Sommer":
        epi = 4          # Epilimnion-Tiefe [m]
        thermo = 6       # Thermokline-Dicke [m]  (4–10 m)
        if depth <= epi:                     # Epilimnion
            return surface_temp - 0.2 * depth        # ~0.2 °C/m
        if depth <= epi + thermo:           # Thermokline
            return (surface_temp
                    - 0.2 * epi             # Abkühlung Epilimnion
                    - 1.0 * (depth - epi))  # ≈1 °C/m in der Thermokline
        return 4.0                          # Hypolimnion

    # ─ Winter: inverse Schichtung unter Eis ──────────────────
    if season == "Winter":
        if depth <= 0.5:                    # 0–50 cm: Eis/Wasserfilm
            return max(0.1, surface_temp)   # nicht unter 0 °C
        return 4.0                          # darunter fast konstant 4 °C

    # ─ Frühling & Herbst: Vollzirkulation ────────────────────
    # (Isotherm – Oberfläche ≈ Tiefe)
    return surface_temp

def get_season(month: int) -> str:
    if month in [3, 4, 5]:
        return "Frühling"
    elif month in [6, 7, 8]:
        return "Sommer"
    elif month in [9, 10, 11]:
        return "Herbst"
    else:
        return "Winter"



# -----------------------------------------------------------------------
# Kombiniertes Temperatur- und Tiefen-Scoring
# -----------------------------------------------------------------------
def score_temp_and_depth(entry, pref, month, weights):
    surf_temp = entry.get("Oberfläche_temp")
    if surf_temp is None:
        return {
            "temp_score": 0.0,
            "depth_score": 0.0,
            "match_score": 0.0,
            "temp_at_depth": None,
            "actual_depth": None,
            "match": False,
            "diff_t": None,
            "min_diff_d": None
        }

    season = get_season(month)
    if season in ["Frühling", "Herbst"]:
        preferred_depths = pref.get("Bevorzugte_Wassertiefe_Frühling_Herbst", [])
    elif season == "Sommer":
        preferred_depths = pref.get("Bevorzugte_Wassertiefe_Sommer", [])
    else:
        preferred_depths = pref.get("Bevorzugte_Wassertiefe_Winter", [])

    actual_depth = entry.get("actual_depth")
    if actual_depth is None:
        depth_values = [float(d) for d in preferred_depths]
        actual_depth = sum(depth_values) / len(depth_values) if depth_values else 0.0

    season = get_season(month)
    temp_at_depth = temp_profile(actual_depth, surf_temp, season)

    prefs_t = [float(x) for x in pref.get("Bevorzugte_Wassertemperatur", [])]
    avg_pref = sum(prefs_t) / len(prefs_t) if prefs_t else temp_at_depth
    diff_t = abs(temp_at_depth - avg_pref)
    temp_weight = weights.get("Temperatur", 0.0)
    if diff_t <= 0.5:
        temp_score = temp_weight
    elif diff_t <= 1.5:
        temp_score = temp_weight * 0.75
    elif diff_t <= 3.0:
        temp_score = temp_weight * 0.5
    elif diff_t <= 5.0:
        temp_score = temp_weight * 0.25
    else:
        temp_score = 0.0

    if preferred_depths:
        min_diff_d = min(abs(actual_depth - float(d)) for d in preferred_depths)
        depth_weight = weights.get("Wassertiefe", 0.0)
        if min_diff_d <= 0.5:
            depth_score = depth_weight
        elif min_diff_d <= 1.0:
            depth_score = depth_weight * 0.5
        else:
            depth_score = 0.0
    else:
        min_diff_d = None
        depth_score = 0.0

    match = (temp_score > 0) and (depth_score > 0)
    match_score = weights.get("TempTiefe_Match", 0.0) if match else 0.0

    return {
        "temp_score":    temp_score,
        "depth_score":   depth_score,
        "match_score":   match_score,
        "temp_at_depth": temp_at_depth,
        "actual_depth":  actual_depth,
        "match":         match,
        "diff_t":        diff_t,
        "min_diff_d":    min_diff_d
    }



# -----------------------------------------------------------------------
# Daten laden
# -----------------------------------------------------------------------
def load_preferences(json_path: str = PREF_PATH) -> Dict[str, Any]:
    with open(json_path, encoding="utf-8") as f:
        data = json.load(f)
    return {it["Art"]: it for it in data} if isinstance(data, list) else data

def load_sensor_data() -> List[Dict[str, Any]]:
    logger.debug("Lade Sensordaten …")
    return BasicSensor.get_consolidated_sensor_data()

# -----------------------------------------------------------------------
# Druck-Trend & Cleaning
# -----------------------------------------------------------------------
def clean_and_calculate(data_list: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    prev: Dict[str, float] = {}
    if os.path.exists(PREV_PRESS_FILE):
        try:
            prev = json.load(open(PREV_PRESS_FILE, encoding="utf-8"))
        except Exception:
            prev = {}
    newp: Dict[str, float] = {}
    for e in data_list:
        art = e.get("Art")
        curr = e.get("Luftdruck") or e.get("ermittelter Luftdruck")
        try:
            curr = float(curr)
        except Exception:
            curr = None
        old = prev.get(art)
        try:
            old = float(old)
        except Exception:
            old = None
        if curr is None or old is None:
            trend = "unbekannt"
        elif curr > old:
            trend = "steigend"
        elif curr < old:
            trend = "fallend"
        else:
            trend = "stagnierend"
        e["Luftdruck_trend"] = trend
        if curr is not None:
            newp[art] = curr
    try:
        json.dump(newp, open(PREV_PRESS_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    except Exception as ex:
        logger.error(f"Fehler Speichern: {ex}")
    return data_list

# -----------------------------------------------------------------------
# Dynamische Zeitfenster
# -----------------------------------------------------------------------
def build_time_windows(sunrise: datetime, sunset: datetime, prefs: List[str]) -> Dict[str, Tuple[datetime, datetime]]:
    b = BUFFERS
    dawn = timedelta(minutes=b.get("Dämmerung", 45))
    day = timedelta(minutes=b.get("Tag", 30))
    night = timedelta(minutes=b.get("Nacht", 30))
    w: Dict[str, Tuple[datetime, datetime]] = {}
    if "Morgen" in prefs:
        w["Morgen"] = (sunrise - dawn, sunrise + dawn)
    if "Tag" in prefs:
        w["Tag"] = (sunrise + day, sunset - day)
    if "Abend" in prefs:
        w["Abend"] = (sunset - dawn, sunset + dawn)
    if "Nacht" in prefs:
        w["Nacht"] = (sunset + night, sunrise + timedelta(days=1) - night)
    return w


def score_time_of_day(now: datetime, sunrise: datetime, sunset: datetime, pref: Dict[str, Any]) -> Tuple[float, Dict[str, Tuple[datetime, datetime]]]:
    prefs = pref.get("Bevorzugte_Tageszeit", [])
    ws = WEIGHTS
    wins = build_time_windows(sunrise, sunset, prefs)
    sc = 0.0
    half = timedelta(hours=1)
    logger.debug(f"[Score-Tageszeit] Jetzt: {now}")
    logger.debug(f"[Score-Tageszeit] Zeitfenster: { {k: (s.strftime('%H:%M'), e.strftime('%H:%M')) for k, (s, e) in wins.items()} }")
    for typ, (s, e) in wins.items():
        if time_in_window(now, s, e):
            logger.debug(f"[Score-Tageszeit] {typ}: Im Zeitfenster! +{ws.get('Tageszeitfenster', 0)} Punkte")
            sc += ws.get("Tageszeitfenster", 0)
        elif time_in_window(now, s - half, s) or time_in_window(now, e, e + half):
            logger.debug(f"[Score-Tageszeit] {typ}: Am Rand des Zeitfensters! +{ws.get('Tageszeitfenster', 0)*0.5} Punkte")
            sc += ws.get("Tageszeitfenster", 0) * 0.5
    # Nacht-Bonus immer ZUSÄTZLICH!
    if "Nacht" in prefs and "Nacht" in wins and time_in_window(now, *wins["Nacht"]):
        logger.debug(f"[Score-Tageszeit] Nachtfenster aktiv! +{ws.get('Nacht_Boost', 0)} Punkte")
        sc += ws.get("Nacht_Boost", 0)
    return sc, wins



# -----------------------------------------------------------------------
# Score-Berechnung
# -----------------------------------------------------------------------
def compute_catch_probability_and_window(
    sensor_records: List[Dict[str, Any]],
    preferences: Dict[str, Any],
    loc: Dict[str, datetime]
) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    sunrise = loc.get("sunrise")
    sunset = loc.get("sunset")

    # 0. Einmalige Zeit-Metadaten
    now = datetime.now(_TZ)
    today = (sunrise or now).date()
    prev_m = 12 if today.month == 1 else today.month - 1
    next_m = 1 if today.month == 12 else today.month + 1

    # 1. Normierung aller Gewichte
    total_weight = sum(WEIGHTS.values()) or 1.0

    for rec in sensor_records:
        art  = rec.get("Art")
        pref = preferences.get(art, {})

        # 2. Temperatur & Tiefe (einmal pro Datensatz)
        parts = score_temp_and_depth(rec, pref, today.month, WEIGHTS)
        rec["Errechnete_Wassertemperatur"] = parts["temp_at_depth"]
        rec["Errechnete_Wassertiefe"]       = parts["actual_depth"]
        rec["TempTiefe_Match"]              = parts["match"]
        score = parts["match_score"]
        logger.debug(f"[DEBUG] Nach TempTiefe: {score}")

        # 3. Windrichtung
        grad = rec.get("windBearing")
        wind_dir = (
            grad_to_windrichtung(grad)
            if isinstance(grad, (int, float))
            else rec.get("Windrichtung", "unbekannt")
        )
        rec["Windrichtung"] = wind_dir
        pref_wind = [d.lower() for d in pref.get("Bevorzugte_Windrichtung", [])]
        if "alle" in pref_wind or wind_dir.lower() in pref_wind:
            w = WEIGHTS.get("Windrichtung", 0)
            score += w
            logger.debug(f"[DEBUG] Nach Windrichtung (+{w}): {score}")

        # 4. Trübung
        tb = classify_trübung(rec)
        rec["Trübung"] = tb
        if tb in pref.get("Trübung", []):
            w = WEIGHTS.get("Trübung", 0)
            score += w
            logger.debug(f"[DEBUG] Nach Trübung (+{w}): {score}")

        # 5. Windig (Geschwindigkeit)
        wind = rec.get("Windgeschwindigkeit jetzt") \
             or rec.get("Windgeschwindigkeit", 0.0)
        w = WEIGHTS.get("Windig", 0)
        if "windig" in pref.get("Bevorzugte_Wetter", []) and wind >= 4:
            factor = min(1.0, (wind - 4) / 4)
            score += w * factor
            logger.debug(f"[DEBUG] Nach Windig linear (+{w*factor:.1f}): {score}")

        # 6. Mondphase
        mond_ist  = rec.get("Mondphase", "").lower()
        mond_pref = [m.lower() for m in pref.get("Bevorzugte_Mondphase", [])]
        w = WEIGHTS.get("Mondphase", 0)
        if "alle" in mond_pref or mond_ist in mond_pref:
            score += w
            logger.debug(f"[DEBUG] Nach Mondphase (+{w}): {score}")
        elif "halbmond" in mond_pref and rec.get("Mondphase") in HALF_MOONS:
            score += w
            logger.debug(f"[DEBUG] Nach Halbmond (+{w}): {score}")

        # 7. Saison
        saison_monate = [
        int(m) for m in pref.get("Beste_Fangsaison", [])
        if isinstance(m, int) or (isinstance(m, str) and m.isdigit())
        ]
        if today.month in saison_monate:
            w = WEIGHTS.get("Saison", 0)
            score += w
            logger.debug(f"[DEBUG] Saison-Volltreffer (+{w}): {score}")
        elif prev_m in saison_monate or next_m in saison_monate:
            w = WEIGHTS.get("Saison", 0) * 0.5
            score += w
            logger.debug(f"[DEBUG] Saison-Halbtreffer (+{w}): {score}")

        # 8. Bewölkung
        cloud_pref = pref.get("Bevorzugte_Wetter", [])
        wp = []
        for lw in cloud_pref:
            lw_l = lw.lower()
            if lw_l == "klar":
                wp += ["klar", "fast klar"]
            elif lw_l in ("dunstig", "neblig"):
                wp.append("wechselhaft")
            else:
                wp.append(lw_l)
        cs = classify_clouds(rec.get("cloudFraction", 0.0))
        is_night = bool(sunrise and sunset and (now < sunrise or now > sunset))
        if not is_night and cs in wp:
            w = WEIGHTS.get("Bewölkung", 0)
            score += w
            logger.debug(f"[DEBUG] Nach Bewölkung (+{w}): {score}")

        # 9. Regen-Bonus
        prec = rec.get("precipIntensity", 0.0)
        if classify_precip(prec) == "regen" and pref.get("Regen", False):
            b = WEIGHTS.get("Regen_Bonus", 0)
            score += b
            logger.debug(f"[DEBUG] Nach Regen-Bonus (+{b}): {score}")

        # 10. Luftdrucktrend
        trend       = rec.get("Luftdruck_trend", "unbekannt").lower()
        raw_prefs   = pref.get("Bevorzugter_Luftdrucktrend", [])
        trend_prefs = [TREND_MAP.get(r.lower(), r.lower()) for r in raw_prefs]
        w = WEIGHTS.get("Luftdruck_trend", 0)
        if trend in trend_prefs:
            score += w
            logger.debug(f"[DEBUG] Nach Drucktrend (+{w}): {score}")
        else:
            for raw in raw_prefs:
                if TREND_MAP.get(raw.lower(), raw.lower()) == trend and raw.lower().startswith("leicht"):
                    pt = w * 0.4
                    score += pt
                    logger.debug(f"[DEBUG] Nach leicht fallend (+{pt:.1f}): {score}")
                    break

        # 11. Dynamisches Fangzeitfenster
        ts_score, window = score_time_of_day(now, sunrise, sunset, pref)
        score += ts_score
        rec["Bestes_Fangfenster"] = {
            k: (s.strftime("%H:%M"), e.strftime("%H:%M"))
            for k, (s, e) in window.items()
        }
        logger.debug(f"[DEBUG] Nach Fangfenster (+{ts_score:.2f}): {score}")

        # 12. Nacht-Boost
        if is_night and "Nacht" in pref.get("Bevorzugte_Tageszeit", []):
            n = WEIGHTS.get("Nacht_Boost", 0)
            score += n
            logger.debug(f"[DEBUG] Nach Nacht-Boost (+{n}): {score}")

        # 13. Finaler Prozentwert
        raw_prob  = round_to_next_five(score / total_weight * 100)
        final_prob = max(10, raw_prob) if score > 0 else 0

        # 14. Regen-Malus
        if prec > 5:
            m = WEIGHTS.get("Regen_Malus", 0)
            final_prob = max(0, final_prob - m)
            logger.debug(f"[DEBUG] Nach Regen-Malus (-{m}): {final_prob}")

        results.append({**rec, "Fangwahrscheinlichkeit_%": final_prob})

    return results



# -----------------------------------------------------------------------
# Sonnenzeiten & Main
# -----------------------------------------------------------------------
def get_sun_times(lat: str, lon: str, tz_name: str) -> Tuple[Optional[datetime], Optional[datetime]]:
    obs = ephem.Observer()
    obs.lat = lat; obs.lon = lon; obs.date = datetime.utcnow()
    sun = ephem.Sun(); now = obs.date
    try:
        sr = obs.previous_rising(sun, use_center=True)
        if now < sr: sr = obs.next_rising(sun, use_center=True)
    except:
        sr = None
    try:
        ss = obs.previous_setting(sun, use_center=True)
        if now > ss: ss = obs.next_setting(sun, use_center=True)
    except:
        ss = None
    def to_local(ed: Optional[ephem.Date]) -> Optional[datetime]:
        if ed is None: return None
        return ed.datetime().replace(tzinfo=timezone.utc).astimezone(ZoneInfo(tz_name))
    return to_local(sr), to_local(ss)


def main() -> List[Dict[str, Any]]:
    prefs = load_preferences()
    raw = load_sensor_data()
    cleaned = clean_and_calculate(raw)
    lat, lon = os.getenv("LATITUDE"), os.getenv("LONGITUDE")
    tz_name = os.getenv("TZ", "Europe/Berlin")
    try:
        sunrise, sunset = get_sun_times(lat, lon, tz_name) if lat and lon else (None, None)
        logger.debug(f"[Sonnenzeiten] Sonnenaufgang: {sunrise}, Sonnenuntergang: {sunset}, Jetzt: {datetime.now(_TZ)}")
        logger.debug(f"[Koordinaten] LAT={lat}, LON={lon}, TZ={tz_name}")
    except:
        today = datetime.now(_TZ).date()
        sunrise = datetime.combine(today, datetime.strptime("05:00","%H:%M").time(), _TZ)
        sunset = datetime.combine(today, datetime.strptime("21:00","%H:%M").time(), _TZ)
        logger.warning("[Sonnenzeiten] Fallback genutzt: Sonnenaufgang 05:00, Sonnenuntergang 21:00")
    return compute_catch_probability_and_window(cleaned, prefs, {"sunrise": sunrise, "sunset": sunset})


if __name__ == "__main__":
    try:
        from mqtt import publish_sensor_data
    except ImportError:
        def publish_sensor_data(*args, **kwargs): pass
    for r in main():
        logger.debug(json.dumps(r, ensure_ascii=False))
        publish_sensor_data(r)
