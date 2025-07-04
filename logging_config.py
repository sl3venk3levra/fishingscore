# logging_config.py
import logging
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

# TZ-Umgebung setzen und anwenden (benötigt tzdata im System)
os.environ.setdefault('TZ', 'Europe/Berlin')
time.tzset()

LOG_FORMAT = "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
# Lese LOG_LEVEL aus ENV, default INFO
LOG_LEVEL = getattr(logging, os.getenv('LOG_LEVEL', 'INFO').upper(), logging.INFO)

class TZFormatter(logging.Formatter):
    """
    Custom Formatter, der Zeitstempel mit ZoneInfo (Europe/Berlin) formatiert.
    """
    def formatTime(self, record, datefmt=None):
        dt = datetime.fromtimestamp(record.created, tz=ZoneInfo("Europe/Berlin"))
        if datefmt:
            return dt.strftime(datefmt)
        # Standardformat entspricht ISO 8601
        return dt.isoformat()

def setup_logging(log_file: str = "analysis.log") -> None:
    """
    Konfiguriert das Root-Logger-System:
    - FileHandler schreibt in analysis.log (Mode 'w')
    - StreamHandler schreibt in die Konsole
    - Beide Handler nutzen den TZFormatter
    """
    file_handler = logging.FileHandler(log_file, mode='w', encoding='utf-8')
    console_handler = logging.StreamHandler()

    formatter = TZFormatter(LOG_FORMAT)
    file_handler.setFormatter(formatter)
    console_handler.setFormatter(formatter)

    root = logging.getLogger()
    # Alte Handler entfernen
    for h in list(root.handlers):
        root.removeHandler(h)
    # Log-Level gemäß ENV
    root.setLevel(LOG_LEVEL)
    root.addHandler(file_handler)
    root.addHandler(console_handler)
