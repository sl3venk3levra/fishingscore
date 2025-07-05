# =============================================================
# mqtt.py – FISCHIS MQTT-Publisher + Forecast-Integration
# Publisht pro Fisch:
#   • % Fangwahrscheinlichkeit  →  …/<fisch>/state
#   • komplette Attribute      →  …/<fisch>/attributes
#   • Fang-Tipps (JSON)        →  …/<fisch>/todo
#   • Forecast (morgen)        →  …/fisch_forecast/<fisch>/* (separates Topic)
# =============================================================

import os
import time
import logging
import json
import signal
import sys
from typing import Dict, Any, List
# ---------------------------------------------
import unicodedata, re             # Slug-Helfer
# ---------------------------------------------

from dotenv import load_dotenv
import paho.mqtt.client as mqtt
from sensor_berechnung import main as load_and_process
from forecast_morgen import forecast_for_tomorrow        # ← NEU

# ---------------------------------------------------------------------------
# Logging & Umgebungs­variablen
# ---------------------------------------------------------------------------
from logging_config import setup_logging

load_dotenv()          # .env zuerst laden, damit LOG_LEVEL greift
setup_logging()        # Logging gemäß LOG_LEVEL konfigurieren
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# MQTT-Einstellungen (per .env übersteuerbar)
# ---------------------------------------------------------------------------
BROKER         = os.getenv("MQTT_BROKER", "127.0.0.1")
PORT           = int(os.getenv("MQTT_PORT", 1883))
USER           = os.getenv("MQTT_USER")
PASSWORD       = os.getenv("MQTT_PASS")
DISCOVERY_ROOT = os.getenv("MQTT_DISCOVERY_PREFIX", "homeassistant")
BASE_TOPIC     = f"{DISCOVERY_ROOT}/sensor/fisch"
FORECAST_BASE_TOPIC = f"{DISCOVERY_ROOT}/sensor/fisch_forecast"   # ← NEU
LOOP_INTERVAL  = int(os.getenv("LOOP_INTERVAL", 600))   # Sekunden

log.debug("→ Verbinde zu MQTT-Broker %r:%s", BROKER, PORT)

# ---------------------------------------------------------------------------
# Hilfsfunktion: ASCII-Slug erzeugen (ä→ae, ö→oe, ü→ue, ß→ss …)
# ---------------------------------------------------------------------------

def slugify(txt: str) -> str:
    txt = (
        unicodedata.normalize("NFKD", txt)
        .encode("ascii", "ignore")
        .decode("ascii")
    )
    txt = re.sub(r"[^a-z0-9_]", "_", txt.lower())
    return re.sub(r"_+", "_", txt).strip("_")

# ---------------------------------------------------------------------------
# MQTT-Client initialisieren (MQTT v5 + Callback-API v2)
# ---------------------------------------------------------------------------
client = mqtt.Client(
    protocol=mqtt.MQTTv5,
    callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
)

if USER:
    client.username_pw_set(USER, PASSWORD)

# ─ Callback-Handler ────────────────────────────────────────────────────────

def on_connect(client, userdata, flags, reason_code, properties):
    if reason_code == 0:
        log.info("✅ Verbunden mit MQTT-Broker %s:%s", BROKER, PORT)
    else:
        log.error("❌ Verbindung fehlgeschlagen (Reason: %s)", reason_code)


def on_publish(client, userdata, mid, reason_code, properties):
    if reason_code == 0:
        log.debug("→ Nachricht %s erfolgreich veröffentlicht", mid)
    else:
        log.warning(
            "→ Nachricht %s Veröffentlichung fehlgeschlagen (Reason: %s)",
            mid,
            reason_code,
        )


client.on_connect = on_connect
client.on_publish = on_publish

# ─ Graceful-Shutdown-Handler ───────────────────────────────────────────────

def _graceful_exit(signum, frame):
    log.info("Shutdown-Signal (%s) empfangen – MQTT sauber beenden …", signum)
    try:
        client.loop_stop()
        client.disconnect()
    finally:
        sys.exit(0)


signal.signal(signal.SIGTERM, _graceful_exit)
signal.signal(signal.SIGINT, _graceful_exit)

# ─ Verbindung aufbauen & Netzwerk-Thread starten ──────────────────────────
client.connect(BROKER, PORT, keepalive=60)
client.loop_start()

# ---------------------------------------------------------------------------
# Discovery & Daten-Publishing
# ---------------------------------------------------------------------------
_published_config: set[str] = set()


def _topic_base(slug: str, forecast: bool) -> str:
    """Liefert den Topic-Prefix für Status/Attribute/Todo."""
    root = FORECAST_BASE_TOPIC if forecast else BASE_TOPIC
    return f"{root}/{slug}"


def publish_discovery(art: str, *, forecast: bool = False) -> None:
    """Veröffentlicht Home-Assistant-Discovery-Blöcke (Status + Todo)."""
    raw_slug = slugify(art)
    slug = f"{raw_slug}_tomorrow" if forecast else raw_slug

    base = _topic_base(slug, forecast)

    # Namens-/ID-Suffixe
    name_suffix = " Morgen" if forecast else ""
    uid_suffix = "_tomorrow" if forecast else ""

    # ─ Prozent-Sensor ────────────────────────────────────────────────────
    topic = f"{base}/config"
    if topic not in _published_config:
        _published_config.add(topic)

        cfg: Dict[str, Any] = {
            "name": f"{art}{name_suffix}",
            "unique_id": f"fischsensor_{raw_slug}{uid_suffix}",
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

        client.publish(topic, json.dumps(cfg, ensure_ascii=False), qos=0, retain=True)
        log.info("→ Discovery publiziert (Status) für %s%s", art, " (Forecast)" if forecast else "")

    # ─ Tipps-Sensor ───────────────────────────────────────────────────────
    todo_topic = f"{base}/todo/config"
    if todo_topic in _published_config:
        return
    _published_config.add(todo_topic)

    todo_cfg: Dict[str, Any] = {
        "name": f"{art}{name_suffix}-Tipps",
        "unique_id": f"fischsensor_{raw_slug}{uid_suffix}_todo",
        "state_topic": f"{base}/todo",
        "json_attributes_topic": f"{base}/todo",
        "icon": "mdi:lightbulb-on-outline",
        "device_class": "diagnostic",
        "entity_category": "diagnostic",
        "value_template": "{{ value_json.todo_count }}",
        "device": {
            "identifiers": ["fischsensor"],
        },
    }

    client.publish(todo_topic, json.dumps(todo_cfg, ensure_ascii=False), qos=0, retain=True)
    log.info("→ Discovery publiziert (Tipps) für %s%s", art, " (Forecast)" if forecast else "")


# ---------------------------------------------------------------------------

def publish_data(art: str, entry: Dict[str, Any], *, forecast: bool = False) -> None:
    """Publisht Status, Attribute und Tipps für Today- oder Forecast-Eintrag."""

    slug = slugify(art) + ("_tomorrow" if forecast else "")
    base = _topic_base(slug, forecast)

    # 1) Attribute
    client.publish(
        f"{base}/attributes",
        json.dumps(entry, ensure_ascii=False),
        qos=0,
        retain=True,
    )
    log.debug("🛈 Attributes gesendet & retained für %s%s", art, " (Forecast)" if forecast else "")

    # 2) Status-Prozent
    client.publish(
        f"{base}/state",
        json.dumps({"status": entry.get("Fangwahrscheinlichkeit_%", 0)}),
        qos=0,
        retain=True,
    )
    log.info(
        "→ Status gesendet & retained für %s%s (%s %%)",
        art,
        " (Forecast)" if forecast else "",
        entry.get("Fangwahrscheinlichkeit_%", 0),
    )

    # 3) Tipps-Sensor (JSON)
    todo_payload = {
        "todo_count": len(entry.get("Verbesserungen", {})),
        "tipps_text": entry.get("Tipps"),
        "verbesserungen": entry.get("Verbesserungen", {}),
    }
    client.publish(
        f"{base}/todo",
        json.dumps(todo_payload, ensure_ascii=False),
        qos=0,
        retain=True,
    )
    log.debug(
        "🛈 Tipps gesendet & retained für %s%s (offen: %s)",
        art,
        " (Forecast)" if forecast else "",
        todo_payload["todo_count"],
    )


# ---------------------------------------------------------------------------
# Hauptschleife
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    try:
        while True:
            # ───── Heutige Messwerte ──────────────────────────────────────
            log.info("Hole verarbeitete Sensordaten (Heute)…")
            today_entries: List[Dict[str, Any]] = list(load_and_process())
            for entry in today_entries:
                art = entry.get("Art", "unbekannt").split(",", 1)[0]
                publish_discovery(art)
                publish_data(art, entry)

            # ───── Forecast für morgen ────────────────────────────────────
            log.info("Berechne Forecast für morgen…")
            tomorrow_entries = forecast_for_tomorrow()
            for entry in tomorrow_entries:
                art = entry.get("Art", "unbekannt").split(",", 1)[0]
                publish_discovery(art, forecast=True)
                publish_data(art, entry, forecast=True)

            log.info("Warte %s s …", LOOP_INTERVAL)
            time.sleep(LOOP_INTERVAL)

    except KeyboardInterrupt:
        _graceful_exit(signal.SIGINT, None)
