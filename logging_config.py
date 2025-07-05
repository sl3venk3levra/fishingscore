# logging_config.py
# =============================================================
import logging
import os
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

# Zeitzone festlegen
os.environ.setdefault("TZ", "Europe/Berlin")
time.tzset()

LOG_FORMAT = "%(asctime)s | %(levelname)-5s | %(name)s | %(message)s"


class TZFormatter(logging.Formatter):
    """Formatter mit ISO-Zeitstempeln in Europe/Berlin."""
    def formatTime(self, record, datefmt=None):
        dt = datetime.fromtimestamp(record.created,
                                    tz=ZoneInfo("Europe/Berlin"))
        return dt.strftime(datefmt) if datefmt else dt.isoformat()


def setup_logging(log_file: str = "analysis.log") -> None:
    """
    Richtet File- und Stream-Handler ein.
    Schaltet bei LOG_LEVEL=OFF|NONE|DISABLE alles stumm.
    """
    raw_level = os.getenv("LOG_LEVEL", "INFO").upper()

    # --- OFF / NONE / DISABLE  → komplette Funkstille -----------------------
    if raw_level in {"OFF", "NONE", "DISABLE"}:
        logging.disable(logging.CRITICAL)
        return

    # --- normales Logging ---------------------------------------------------
    log_level = getattr(logging, raw_level, logging.INFO)

    file_handler = logging.FileHandler(log_file, mode="w",
                                       encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)          # immer alles in Datei

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(log_level)           # Konsole gefiltert

    fmt = TZFormatter(LOG_FORMAT)
    file_handler.setFormatter(fmt)
    console_handler.setFormatter(fmt)

    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)

    root.setLevel(logging.DEBUG)                  # interner Basis-Level
    root.addHandler(file_handler)
    root.addHandler(console_handler)
