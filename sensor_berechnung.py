from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
import math
import ephem
from typing import Final
from zoneinfo import ZoneInfo
from logging_config import setup_logging
from basic_sensor import BasicSensor
from math import exp

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
            "Saison":             8,
            "Temperatur":        15,
            "Wassertiefe":       10,
            "TempTiefe_Match":   15,
            "Tageszeitfenster":  12,
            "Nacht_Boost":        6,
            "Luftdruck_trend":    6,
            "Windrichtung":       3,
            "Windig":             4,
            "Bewölkung":          4,
            "Regen_Bonus":        4,
            "Regen_Malus":        4,
            "Mondphase":          4,
            "Trübung":            4
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

def _dt(date_: datetime, hours: float) -> datetime:
    """Hilfsfunktion: date_ + hours (Positiv = vorwärts, Negativ = rückwärts)."""
    return date_ + timedelta(hours=hours)

def _clamp(val: float, low: float, high: float) -> float:
    """Begrenzt val auf das Intervall [low, high]."""
    return max(low, min(high, val))

# Feature-Flag: 1 = dynamisches Modell, 0 = statisches 4-m-Modell
_DYNAMIC_EPI: Final[bool] = bool(int(os.getenv("DYNAMIC_EPI", "1")))


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

def get_season(month: int) -> str:
    if month in [3, 4, 5]:
        return "Frühling"
    elif month in [6, 7, 8]:
        return "Sommer"
    elif month in [9, 10, 11]:
        return "Herbst"
    else:
        return "Winter"


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

def gauss_score(diff: float, weight: float, sigma: float = 2.0) -> float:
    """
    Skaliert eine Gauß-Kurve (μ=0) auf 0..weight.
    diff  : Abstand zum Ideal (>=0)
    sigma : Breite der Akzeptanzzone
    """
    if sigma <= 0 or weight <= 0:
        return 0.0
    return weight * math.exp(-(diff ** 2) / (2 * sigma ** 2))
    

def temp_profile(depth: float,
                 surface_temp: float,
                 season: str,
                 wind_speed: float | None = None,
                 cloud: float | None = None) -> float:
    """
    Realistische Temperaturabschätzung in 'depth' Metern.
    Berücksichtigt Saison, Oberflächentemperatur, Wind und (optional) Bewölkung.
    """

    wind = wind_speed or 0.0      # m s-1
    cloud = cloud or 0.0          # 0–1, beeinflusst lediglich Frühling/Herbst (optional)

    # ───────────────────   SOMMER: stabile Schichtung   ────────────────────
    if season == "Sommer":
        # 1) Epilimnion-Tiefe: typ. 2 – 6 m, etwas tiefer bei Wind
        epi = _clamp(
            0.2 * (surface_temp - 4.0) + 0.2 * _clamp(wind, 0, 5),  # Basis + Wind
            2.0, 6.0
        )

        # 2) Thermokline-Dicke: fix 3 m + leichter Wind-Aufschlag (max 5 m)
        thermo = _clamp(3.0 + 0.3 * _clamp(wind, 0, 5), 3.0, 5.0)

        # 3) Temperatur-Gradienten (Literaturwerte)
        grad_epi   = 0.2                    # °C pro Meter im Epilimnion
        grad_therm = 1.2                    # °C pro Meter in der Thermokline

        if depth <= epi:                    # Epilimnion
            return surface_temp - depth * grad_epi

        if depth <= epi + thermo:           # Thermokline
            return (surface_temp - epi * grad_epi
                    - (depth - epi) * grad_therm)

        # 4) Hypolimnion – linearer Abfall bis min. 4 °C
        hypo_start = surface_temp - epi * grad_epi - thermo * grad_therm
        return max(hypo_start - (depth - epi - thermo) * 0.3, 4.0)

    # ───────────────────   FRÜHLING / HERBST (Vollmischung)   ───────────────
    if season in {"Frühling", "Herbst"}:
        # Bewölkte Tage mischen weniger (leicht kälter), sonnige Tage etwas wärmer
        cloud_factor = 0.5 * (1 - cloud)          # 0 (bewölkt) … 0,5 (klar)
        return surface_temp + cloud_factor

    # ───────────────────   WINTER (Inverse Schichtung)   ────────────────────
    # Eisdecke oder sehr kaltes Oberflächenwasser
    if season == "Winter":
        if depth <= 0.5:                          # 0–50 cm: Eis/Nahoberfläche
            return max(0.1, surface_temp)
        return 4.0                                # darunter ca. 4 °C

    # Fallback: einfach Oberfläche
    return surface_temp

# -----------------------------------------------------------------------
# Helfer: Schichtung & Tiefenwahl
# -----------------------------------------------------------------------
def stratification_layers(surface_temp: float, season: str, wind: float = 0.0
                          ) -> tuple[float, float]:
    """Realistische Epi- & Thermokline-Tiefe (Sommer); sonst ∞."""
    if season != "Sommer":
        return float("inf"), 0.0

    epi = _clamp(0.2 * (surface_temp - 4.0) + 0.2 * _clamp(wind, 0, 5),
                 2.0, 6.0)                         # 2–6 m
    thermo = _clamp(3.0 + 0.3 * _clamp(wind, 0, 5),
                    3.0, 5.0)                      # 3–5 m
    return epi, thermo


def choose_best_depth(surface_temp: float, season: str, pref_depths: list[float], pref_temps: list[float], tod_bias: str | None = None, *, wind_speed: float = 0.0,
    cloud: float = 0.0,
) -> float:
    
    """Ermittelt beste Tiefe – temperatur- und schichtoptimiert, mit optionalem Tageszeit-Bias."""
    if not pref_depths:
        return 0.0

    epi, thermo = stratification_layers(surface_temp, season, wind_speed)
    limit = epi + thermo
    candidates = [d for d in pref_depths if d <= limit]
    if not candidates:
        candidates = pref_depths

    target = sum(pref_temps) / len(pref_temps) if pref_temps else surface_temp
    best = min(
        candidates,
        key=lambda d: abs(
            temp_profile(
                d, surface_temp, season,
                wind_speed=wind_speed, cloud=cloud
            ) - target
        ),
    )

    if tod_bias == "flach" and best > min(pref_depths):
        best = max(min(pref_depths), best - 0.5)
    elif tod_bias == "tief" and best < max(pref_depths):
        best = min(max(pref_depths), best + 0.5)

    return float(best)



# -----------------------------------------------------------------------
# Kombiniertes Temperatur- und Tiefen-Scoring
# -----------------------------------------------------------------------
def score_temp_and_depth(entry, pref, month, weights):
    """Berechnet Teil-Scores für Temperatur und Wassertiefe
       und gibt ein Dict mit allen Zwischenergebnissen zurück.
    """
    # ───── Eingangsdaten prüfen ───────────────────────────────────────────
    surf_temp = entry.get("Oberfläche_temp")
    wind  = float(entry.get("Windgeschwindigkeit", 0.0))
    cloud = float(entry.get("cloudFraction",      0.0))

    if surf_temp is None:
        return {
            "temp_score": 0.0, "depth_score": 0.0, "match_score": 0.0,
            "temp_at_depth": None, "actual_depth": None,
            "match": False, "diff_t": None, "min_diff_d": None,
        }

    # ───── Saison & Tiefen­präferenzen ────────────────────────────────────
    season = get_season(month)
    depth_key = {
        "Frühling": "Bevorzugte_Wassertiefe_Frühling_Herbst",
        "Herbst":   "Bevorzugte_Wassertiefe_Frühling_Herbst",
        "Sommer":   "Bevorzugte_Wassertiefe_Sommer",
        "Winter":   "Bevorzugte_Wassertiefe_Winter",
    }[season]
    preferred_depths = pref.get(depth_key, [])

    # ───── effektive Tiefe bestimmen ──────────────────────────────────────
    actual_depth = entry.get("actual_depth")
    if actual_depth is None:
        now        = datetime.now(tz=_TZ)
        bias_times = ["Morgen", "Abend", "Nacht"]
        precip     = float(entry.get("precipIntensity", 0.0))

        tod_bias = "flach" if any(
            time_in_window(now, *w)
            for w in build_time_windows(
                    now, now, bias_times,
                    cloud=cloud, wind=wind, precip=precip
                ).values()
        ) else None

        actual_depth = choose_best_depth(
            surf_temp, season,
            [float(d) for d in preferred_depths],
            [float(t) for t in pref.get("Bevorzugte_Wassertemperatur", [])],
            tod_bias=tod_bias,
            wind_speed=wind,
            cloud=cloud,
        )

    # ───── Temperatur an dieser Tiefe ─────────────────────────────────────
    temp_at_depth = temp_profile(
        actual_depth, surf_temp, season,
        wind_speed=wind, cloud=cloud,
    )

    # ───── Temperatur-Score (Gauß) ────────────────────────────────────────
    prefs_t = [float(x) for x in pref.get("Bevorzugte_Wassertemperatur", [])]
    if prefs_t:
        diff_t = min(abs(temp_at_depth - p) for p in prefs_t)
        sigma  = 0.25 * (max(prefs_t) - min(prefs_t) or 1.0)
    else:
        diff_t = abs(temp_at_depth - surf_temp)
        sigma  = 2.0

    temp_weight = weights.get("Temperatur", 0.0)
    temp_score  = gauss_score(diff_t, temp_weight, sigma)
    MIN_TEMP_SCORE = 0.3 * temp_weight
    temp_ok = temp_score >= MIN_TEMP_SCORE

    # ───── Tiefen-Score ───────────────────────────────────────────────────
    if preferred_depths:
        min_diff_d   = min(abs(actual_depth - float(d)) for d in preferred_depths)
        depth_weight = weights.get("Wassertiefe", 0.0)
        if   min_diff_d <= 0.5: depth_score = depth_weight
        elif min_diff_d <= 1.0: depth_score = depth_weight * 0.5
        else:                   depth_score = 0.0
    else:
        min_diff_d, depth_score = None, 0.0

    # ───── Match-Bonus ────────────────────────────────────────────────────
    match       = temp_ok and (depth_score > 0)
    match_score = weights.get("TempTiefe_Match", 0.0) if match else 0.0

    # ───── *** WICHTIG: Ergebnis zurückgeben! *** ─────────────────────────
    return {
        "temp_score":    temp_score,
        "depth_score":   depth_score,
        "match_score":   match_score,
        "temp_at_depth": temp_at_depth,
        "actual_depth":  actual_depth,
        "match":         match,
        "diff_t":        diff_t,
        "min_diff_d":    min_diff_d,
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


def light_modifier(cloud: float, wind: float, precip: float) -> float:
    """
    Liefert einen Wert 0..1 für Tageslicht­helligkeit.
    - cloud        : 0 (klar) – 1 (voll bedeckt)
    - wind         : m/s   (0–8 ⇒ 0–0.2 künstliche Bewölkung)
    - precip       : mm/h  (>0 ⇒ Abdunklung + Geräuschkulisse)
    """
    cloud_eff = cloud + 0.02 * min(wind, 10)        # Wind = max +0.2 Bewölkung
    rain_eff  = 0.15 if precip >= 0.2 else 0.0      # merkbarer Regen
    return max(0.0, 1.0 - _clamp(cloud_eff + rain_eff, 0.0, 1.0))


# -----------------------------------------------------------------------
# Dynamische Zeitfenster
# -----------------------------------------------------------------------
def build_time_windows(
    sunrise: datetime,
    sunset:  datetime,
    prefs:   list[str],
    *,
    cloud:  float,
    wind:   float,
    precip: float,
) -> dict[str, tuple[datetime, datetime]]:
    """
    Liefert (start, end)-Tupel für gewünschte Tageszeiten.
    Puffer werden dynamisch nach Licht­verhältnissen erweitert/
    verkürzt.  start < end gilt immer, auch über Mitternacht.
    """
    # 1) Lichtpegel bestimmen (0 dunkel – 1 hell)
    L = light_modifier(cloud, wind, precip)

    # 2) Basis-Puffer (min)  ➜  h
    buf = BUFFERS
    dawn   = (buf.get("Dämmerung", 45) * (1 + (1 - L))) / 60
    daybuf = (buf.get("Tag", 30) * L) / 60
    nightb = (buf.get("Nacht", 30) * (1 + (1 - L))) / 60

    win: dict[str, tuple[datetime, datetime]] = {}

    if "Morgen" in prefs:
        win["Morgen"] = (_dt(sunrise, -dawn), _dt(sunrise,  dawn))

    if "Abend" in prefs:
        win["Abend"] = (_dt(sunset, -dawn), _dt(sunset,  dawn))

    if "Tag" in prefs:
        win["Tag"] = (_dt(sunrise,  daybuf), _dt(sunset, -daybuf))

    if "Nacht" in prefs:
        s = _dt(sunset,  nightb)
        e = _dt(sunrise, nightb) + timedelta(days=1)
        win["Nacht"] = (s, e)

    return win



def score_time_of_day(now: datetime,
                      sunrise: datetime,
                      sunset: datetime,
                      pref: Dict[str, Any],
                      *,
                      cloud: float,
                      wind: float,
                      precip: float) -> Tuple[float, Dict[str, Tuple[datetime, datetime]]]:

    prefs = pref.get("Bevorzugte_Tageszeit", [])
    ws = WEIGHTS
    wins = build_time_windows(sunrise, sunset, prefs,cloud=cloud, wind=wind, precip=precip)
    sc = 0.0
    half = timedelta(hours=1)
    logger.debug(f"[Score-Tageszeit] Jetzt: {now}")
    logger.debug(f"[Score-Tageszeit] Zeitfenster: { {k: (s.strftime('%H:%M'), e.strftime('%H:%M')) for k, (s, e) in wins.items()} }")
    for typ, (s, e) in wins.items():
        if time_in_window(now, s, e):
            logger.debug(f"[Score-Tageszeit] {typ}: Im Zeitfenster! +{ws.get('Tageszeitfenster', 0)} Punkte")
            sc += ws.get("Tageszeitfenster", 0) * light_modifier(cloud, wind, precip)
        elif time_in_window(now, s - half, s) or time_in_window(now, e, e + half):
            logger.debug(f"[Score-Tageszeit] {typ}: Am Rand des Zeitfensters! +{ws.get('Tageszeitfenster', 0)*0.5} Punkte")
            sc += ws.get("Tageszeitfenster", 0) * light_modifier(cloud, wind, precip)
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
        ts_score, window = score_time_of_day(
                                            now, sunrise, sunset, pref,
                                            cloud=rec.get("cloudFraction", 0.0),
                                            wind=float(rec.get("Windgeschwindigkeit", 0.0)),
                                            precip=float(rec.get("precipIntensity", 0.0)),
                                        )
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
