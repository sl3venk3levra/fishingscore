# =============================================================
# mqtt.py – FISCHIS MQTT-Publisher + Forecast-Integration
# Publisht pro Fisch:
#   • % Fangwahrscheinlichkeit    → …/<fisch>/state
#   • komplette Attribute         → …/<fisch>/attributes
#   • Fang-Tipps (JSON)           → …/<fisch>/todo
#   • Forecast (morgen)           → …/fisch_forecast/<fisch>/*
#   • Top-N-Sensoren („die_drei_besten“)  → …/sensor/die_drei_besten/rang_<i>/*
# =============================================================

from __future__ import annotations
import os, time, logging, json, signal, sys, unicodedata, re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Dict, Any, List

from dotenv import load_dotenv # type: ignore
import paho.mqtt.client as mqtt # type: ignore
from sensor_berechnung import main as load_and_process
from forecast_morgen import forecast_for_tomorrow
from logging_config import setup_logging

# ---------------------------------------------------------------------------
# Logging & Env
# ---------------------------------------------------------------------------
load_dotenv()
setup_logging()
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# MQTT & Config
# ---------------------------------------------------------------------------
BROKER               = os.getenv("MQTT_BROKER", "127.0.0.1")
PORT                 = int(os.getenv("MQTT_PORT", 1883))
USER                 = os.getenv("MQTT_USER")
PASSWORD             = os.getenv("MQTT_PASS")
DISCOVERY_ROOT       = os.getenv("MQTT_DISCOVERY_PREFIX", "homeassistant")
BASE_TOPIC           = f"{DISCOVERY_ROOT}/sensor/fisch"
FORECAST_BASE_TOPIC  = f"{DISCOVERY_ROOT}/sensor/fisch_forecast"
BEST3_TOPIC          = f"{DISCOVERY_ROOT}/sensor/die_drei_besten"
LOOP_INTERVAL        = int(os.getenv("LOOP_INTERVAL", 600))
NEXT_FISH_COUNT      = int(os.getenv("NEXT_FISH_COUNT", "0"))
_TZ                  = ZoneInfo(os.getenv("TZ", "Europe/Berlin"))

# Tracker für schon publizierte Discovery-Topics
_published_config: set[str] = set()
_published_best3: set[str] = set()
_last_configured: dict[int, str] = {}

log.debug("→ Verbinde zu MQTT-Broker %r:%s", BROKER, PORT)

# ---------------------------------------------------------------------------
# Slug-Helfer
# ---------------------------------------------------------------------------
def slugify(txt: str) -> str:
    txt = unicodedata.normalize("NFKD", txt).encode("ascii", "ignore").decode("ascii")
    txt = re.sub(r"[^a-z0-9_]", "_", txt.lower())
    return re.sub(r"_+", "_", txt).strip("_")

# ---------------------------------------------------------------------------
# MQTT-Client
# ---------------------------------------------------------------------------
client = mqtt.Client(protocol=mqtt.MQTTv5, callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
if USER:
    client.username_pw_set(USER, PASSWORD)

def on_connect(client, userdata, flags, reason_code, properties):
    if reason_code == 0:
        log.info("✅ Verbunden mit MQTT-Broker %s:%s", BROKER, PORT)
    else:
        log.error("❌ Verbindung fehlgeschlagen (Reason: %s)", reason_code)

def on_publish(client, userdata, mid, reason_code, properties):
    if reason_code == 0:
        log.debug("→ Nachricht %s erfolgreich veröffentlicht", mid)
    else:
        log.warning("→ Nachricht %s Veröffentlichung fehlgeschlagen (Reason: %s)", mid, reason_code)

client.on_connect = on_connect
client.on_publish = on_publish

def _graceful_exit(signum, frame):
    log.info("Shutdown-Signal (%s) empfangen – MQTT sauber beenden …", signum)
    try:
        client.loop_stop()
        client.disconnect()
    finally:
        sys.exit(0)

signal.signal(signal.SIGTERM, _graceful_exit)
signal.signal(signal.SIGINT,  _graceful_exit)

client.connect(BROKER, PORT, keepalive=60)
client.loop_start()

# ---------------------------------------------------------------------------
# Discovery & Data-Publishing für Einzel-Fische
# ---------------------------------------------------------------------------
def _topic_base(slug: str, forecast: bool) -> str:
    return f"{FORECAST_BASE_TOPIC if forecast else BASE_TOPIC}/{slug}"

def publish_discovery(art: str, *, forecast: bool = False) -> None:
    raw_slug = slugify(art)
    slug     = f"{raw_slug}_tomorrow" if forecast else raw_slug
    base     = _topic_base(slug, forecast)

    # Prozent-Sensor
    cfg_topic = f"{base}/config"
    if cfg_topic not in _published_config:
        _published_config.add(cfg_topic)
        cfg = {
            "name": f"{art}{' Morgen' if forecast else ''}",
            "unique_id": f"fischsensor_{raw_slug}{'_tomorrow' if forecast else ''}",
            "state_topic": f"{base}/state",
            "json_attributes_topic": f"{base}/attributes",
            "icon": "mdi:fish",
            "unit_of_measurement": "%",
            "state_class": "measurement",
            "value_template": "{{ value_json.status | float }}",
            "device": {
                "identifiers": ["fischsensor"],
                "name": "Fischsensor",
                "model": "Fishing Docker",
                "manufacturer": "Eigenentwicklung",
            },
        }
        client.publish(cfg_topic, json.dumps(cfg, ensure_ascii=False), qos=0, retain=True)
        log.info("→ Discovery publiziert für %s%s", art, " (Forecast)" if forecast else "")

    # Tipps-Sensor
    todo_topic = f"{base}/todo/config"
    if todo_topic not in _published_config:
        _published_config.add(todo_topic)
        todo_cfg = {
            "name": f"{art}{' Morgen' if forecast else ''}-Tipps",
            "unique_id": f"fischsensor_{raw_slug}{'_tomorrow' if forecast else ''}_todo",
            "state_topic": f"{base}/todo",
            "json_attributes_topic": f"{base}/todo",
            "icon": "mdi:lightbulb-on-outline",
            "device_class": "diagnostic",
            "entity_category": "diagnostic",
            "value_template": "{{ value_json.todo_count }}",
            "device": {"identifiers": ["fischsensor"]},
        }
        client.publish(todo_topic, json.dumps(todo_cfg, ensure_ascii=False), qos=0, retain=True)
        log.info("→ Tipps-Discovery publiziert für %s%s", art, " (Forecast)" if forecast else "")

def publish_data(art: str, entry: Dict[str, Any], *, forecast: bool = False) -> None:
    slug = slugify(art) + ("_tomorrow" if forecast else "")
    base = _topic_base(slug, forecast)
    # Attribute
    client.publish(f"{base}/attributes", json.dumps(entry, ensure_ascii=False), qos=0, retain=True)
    # Status
    client.publish(f"{base}/state", json.dumps({"status": entry.get("Fangwahrscheinlichkeit_%", 0)}),
                   qos=0, retain=True)
    # Tipps
    todo = {
        "todo_count": len(entry.get("Verbesserungen", {})),
        "tipps_text": entry.get("Tipps"),
        "verbesserungen": entry.get("Verbesserungen", {}),
    }
    client.publish(f"{base}/todo", json.dumps(todo, ensure_ascii=False), qos=0, retain=True)

# ---------------------------------------------------------------------------
# Top-N Hilfsroutinen
# ---------------------------------------------------------------------------
def _next_window_start(entry: Dict[str, Any]) -> timedelta:
    now = datetime.now(_TZ)
    shortest = timedelta(days=999)
    fenster = entry.get("Bestes_Fangfenster", {})
    iterable = fenster.values() if isinstance(fenster, dict) else fenster if isinstance(fenster, list) else []
    for val in iterable:
        if isinstance(val, (list, tuple)) and val:
            start_str = val[0]
        else:
            continue
        try:
            hh, mm = map(int, start_str.split(":", 1))
            start = datetime(now.year, now.month, now.day, hh, mm, tzinfo=_TZ)
            if start < now:
                start += timedelta(days=1)
            shortest = min(shortest, start - now)
        except Exception:
            continue
    return shortest

def _in_current_window(entry: Dict[str, Any]) -> bool:
    now = datetime.now(_TZ)
    fenster = entry.get("Bestes_Fangfenster", {})
    iterable = fenster.values() if isinstance(fenster, dict) else fenster if isinstance(fenster, list) else []
    for val in iterable:
        if isinstance(val, (list, tuple)) and len(val) >= 2:
            start_str, end_str = val[0], val[1]
        else:
            continue
        try:
            hh1, mm1 = map(int, start_str.split(":", 1))
            hh2, mm2 = map(int, end_str.split(":", 1))
            start = datetime(now.year, now.month, now.day, hh1, mm1, tzinfo=_TZ)
            end = datetime(now.year, now.month, now.day, hh2, mm2, tzinfo=_TZ)
            # über Mitternacht?
            if end <= start:
                if now >= start or now <= end:
                    return True
            else:
                if start <= now <= end:
                    return True
        except Exception:
            continue
    return False

def compute_sorted(entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(
        entries,
        key=lambda e: (
            -e.get("Fangwahrscheinlichkeit_%", 0),
            0 if _in_current_window(e) else 1,
            _next_window_start(e),
        )
    )

def publish_top3(best3: List[Dict[str, Any]]) -> None:
    for idx, b in enumerate(best3, start=1):
        art     = b.get("Art", "unbekannt")
        score   = b.get("Fangwahrscheinlichkeit_%", 0)
        fenster = b.get("Bestes_Fangfenster", {})

        base      = f"{BEST3_TOPIC}/rang_{idx}"
        cfg_topic = f"{base}/config"

        # Discovery nur senden, wenn die Art auf diesem Rang gewechselt hat
        if _last_configured.get(idx) != art:
            _last_configured[idx] = art
            cfg = {
                "name":      f"Top {idx}: {art}",
                "unique_id": f"fischsensor_top_{idx}",
                "state_topic": f"{base}/state",
                "json_attributes_topic": f"{base}/attributes",
                "icon": "mdi:trophy",
                "unit_of_measurement": "%",
                "state_class": "measurement",
                "value_template": "{{ value_json.status | float }}",
                "device": {"identifiers": ["fischsensor"]},
            }
            client.publish(cfg_topic, json.dumps(cfg, ensure_ascii=False),
                           qos=0, retain=True)
            log.info("→ Discovery (Top %d) neu publiziert: %s", idx, art)

        # State & Attribute immer aktuell
        client.publish(f"{base}/state",
                       json.dumps({"status": score}), qos=0, retain=True)
        client.publish(f"{base}/attributes",
                       json.dumps({"art": art, "score": score, "fenster": fenster},
                                  ensure_ascii=False),
                       qos=0, retain=True)
        log.info("→ Top-%d-Sensor aktualisiert (%s: %s%%)", idx, art, score)



def publish_additional(sorted_entries: List[Dict[str, Any]]) -> None:
    """
    Publisht dynamisch Sensoren von Rang 4 bis Rang (3 + NEXT_FISH_COUNT),
    Discovery-Block nur bei Art-Wechsel.
    """
    for offset, b in enumerate(sorted_entries[3 : 3 + NEXT_FISH_COUNT], start=4):
        art     = b.get("Art", "unbekannt")
        score   = b.get("Fangwahrscheinlichkeit_%", 0)
        fenster = b.get("Bestes_Fangfenster", {})

        base      = f"{BEST3_TOPIC}/rang_{offset}"
        cfg_topic = f"{base}/config"

        # Discovery neu, wenn Fisch gewechselt hat
        if _last_configured.get(offset) != art:
            _last_configured[offset] = art
            cfg = {
                "name":      f"Top {offset}: {art}",
                "unique_id": f"fischsensor_top_{offset}",
                "state_topic": f"{base}/state",
                "json_attributes_topic": f"{base}/attributes",
                "icon": "mdi:trophy",
                "unit_of_measurement": "%",
                "state_class": "measurement",
                "value_template": "{{ value_json.status | float }}",
                "device": {"identifiers": ["fischsensor"]},
            }
            client.publish(cfg_topic, json.dumps(cfg, ensure_ascii=False),
                           qos=0, retain=True)
            log.info("→ Discovery (Top %d) neu publiziert: %s", offset, art)

        # Laufende Daten
        client.publish(f"{base}/state",
                       json.dumps({"status": score}), qos=0, retain=True)
        client.publish(f"{base}/attributes",
                       json.dumps({"art": art, "score": score, "fenster": fenster},
                                  ensure_ascii=False),
                       qos=0, retain=True)
        log.info("→ Top-%d-Sensor aktualisiert (%s: %s%%)", offset, art, score)



# ---------------------------------------------------------------------------
# Hauptschleife
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    try:
        while True:
            log.info("Hole verarbeitete Sensordaten (Heute)…")
            today = list(load_and_process())
            for e in today:
                art = e.get("Art", "unbekannt").split(",", 1)[0]
                publish_discovery(art)
                publish_data(art, e)

            sorted_entries = compute_sorted(today)
            publish_top3(sorted_entries[:3])
            if NEXT_FISH_COUNT > 0:
                publish_additional(sorted_entries) # type: ignore

            log.info("Berechne Forecast für morgen…")
            tomorrow = forecast_for_tomorrow()
            for e in tomorrow:
                art = e.get("Art", "unbekannt").split(",", 1)[0]
                publish_discovery(art, forecast=True)
                publish_data(art, e, forecast=True)

            log.info("Warte %s s …", LOOP_INTERVAL)
            time.sleep(LOOP_INTERVAL)

    except KeyboardInterrupt:
        _graceful_exit(signal.SIGINT, None)
