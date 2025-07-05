# =============================================================
# mqtt.py – FISCHIS MQTT-Publisher (mit % Fangwahrscheinlichkeit als State)
# =============================================================

import os
import time
import logging
import json
import signal           #  ➜ für sauberes Beenden
import sys              #  ➜ für sys.exit
from typing import Dict, Any

from dotenv import load_dotenv
import paho.mqtt.client as mqtt
from sensor_berechnung import main as load_and_process

# ---------------------------------------------------------------------------
# Logging & Umgebungs­variablen
# ---------------------------------------------------------------------------
from logging_config import setup_logging

load_dotenv()        # .env zuerst laden, damit LOG_LEVEL greift
setup_logging()      # Logging gemäß LOG_LEVEL konfigurieren
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
LOOP_INTERVAL  = int(os.getenv("LOOP_INTERVAL", 600))   # Sekunden

log.debug("→ Verbinde zu MQTT-Broker %r:%s", BROKER, PORT)

# ---------------------------------------------------------------------------
# MQTT-Client initialisieren (MQTT v5 + Callback-API v2)
# ---------------------------------------------------------------------------
client = mqtt.Client(
    protocol=mqtt.MQTTv5,
    callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
)

# Zugangsdaten setzen (falls vorhanden)
if USER:
    client.username_pw_set(USER, PASSWORD)

# Automatische Reconnect-Delays (1 – 30 s)
client.reconnect_delay_set(min_delay=1, max_delay=30)

# ─ Callback-Handler ─────────────────────────────────────────────────────────
def on_connect(client, userdata, flags, reason_code, properties):
    if reason_code == 0:
        log.info("✅ Verbunden mit MQTT-Broker %s:%s", BROKER, PORT)
    else:
        log.error("❌ Verbindung fehlgeschlagen zu %s:%s (Reason: %s)",
                  BROKER, PORT, reason_code)

def on_publish(client, userdata, mid, reason_code, properties):
    if reason_code == 0:
        log.debug("→ Nachricht %s erfolgreich veröffentlicht", mid)
    else:
        log.warning("→ Nachricht %s Veröffentlichung fehlgeschlagen (Reason: %s)",
                    mid, reason_code)

client.on_connect = on_connect
client.on_publish = on_publish

# ─ Graceful-Shutdown-Handler ────────────────────────────────────────────────
def _graceful_exit(signum, frame):
    log.info("Shutdown-Signal (%s) empfangen – MQTT sauber beenden …", signum)
    try:
        client.loop_stop()      # Netzwerk-Thread anhalten
        client.disconnect()     # Clean DISCONNECT
    finally:
        sys.exit(0)

# Signale registrieren (Docker / systemd / CTRL-C)
signal.signal(signal.SIGTERM, _graceful_exit)
signal.signal(signal.SIGINT,  _graceful_exit)

# ─ Verbindung aufbauen & Netzwerk-Thread starten ───────────────────────────
client.connect(BROKER, PORT, keepalive=60)
client.loop_start()

# ---------------------------------------------------------------------------
# Discovery & Daten-Publishing
# ---------------------------------------------------------------------------
_published_config: set[str] = set()

def publish_discovery(art: str) -> None:
    """Publish Home-Assistant-Discovery-Config für einen Fisch."""
    topic = f"{BASE_TOPIC}/{art.lower()}/config"
    if topic in _published_config:
        return                     # schon publiziert
    _published_config.add(topic)

    cfg: Dict[str, Any] = {
        "name":                  f"{art}-Sensor",
        "unique_id":             f"fischsensor_{art.lower()}",
        "state_topic":           f"{BASE_TOPIC}/{art.lower()}/state",
        "json_attributes_topic": f"{BASE_TOPIC}/{art.lower()}/attributes",
        "icon":                  "mdi:fish",
        "unit_of_measurement":   "%",
        "state_class":           "measurement",
        "value_template":        "{{ value_json.status | float }}",
        "device": {
            "identifiers":  ["fischsensor"],
            "name":         "Fischsensor",
            "model":        "Fishing Docker",
            "manufacturer": "Eigenentwicklung",
        },
    }

    client.publish(topic, json.dumps(cfg, ensure_ascii=False),
                   qos=0, retain=True)
    log.info("→ Home Assistant Discovery publiziert für %s", art)

def publish_data(art: str, entry: Dict[str, Any]) -> None:
    """Sende Attribute + State (Fangwahrscheinlichkeit) für einen Fisch."""
    base = f"{BASE_TOPIC}/{art.lower()}"

    # 1) Attribute (voller Datensatz)
    client.publish(
        f"{base}/attributes",
        json.dumps(entry, ensure_ascii=False),
        qos=0,
        retain=True,
    )
    log.debug("🛈 Attributes gesendet und retained für %s", art)

    # 2) State – nur die Prozentzahl
    client.publish(
        f"{base}/state",
        json.dumps({"status": entry.get("Fangwahrscheinlichkeit_%", 0)}),
        qos=0,
        retain=True,
    )
    log.info("→ State gesendet und retained für %s (Status = %s%%)",
             art, entry.get("Fangwahrscheinlichkeit_%", 0))

# ---------------------------------------------------------------------------
# Hauptschleife
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    try:
        while True:
            log.info("Hole verarbeitete Sensordaten …")
            for entry in load_and_process():
                art = entry.get("Art", "unbekannt").split(",", 1)[0]
                publish_discovery(art)
                publish_data(art, entry)

            log.info("Warte %s Sekunden …", LOOP_INTERVAL)
            time.sleep(LOOP_INTERVAL)

    except KeyboardInterrupt:
        _graceful_exit(signal.SIGINT, None)
