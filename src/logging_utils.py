"""Logging utilities for consistent log file management.

This module creates a single run-level logfile named with the prefix
"pubchem" and a timestamp accurate to the second when the process starts.
Multiple calls to ``setup_logging`` during the same run will reuse the
same logfile and will not add duplicate handlers.
"""
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from decouple import config

# Compute a single timestamp when the module is imported. This guarantees
# a single filename per program run (accurate to the second).
_RUN_TIMESTAMP = datetime.now().strftime("%m_%d_%Y_%H_%M_%S")
# Cached logfile path for reuse
_LOG_FILE_PATH: Optional[str] = None


def setup_logging(module_name: str = "pubchem") -> Optional[str]:
    """
    Initialize logging once per process and return the logfile path.

    The filename uses the fixed prefix "pubchem" followed by the run
    timestamp so only one logfile is created per run regardless of which
    module calls this function.
    """
    global _LOG_FILE_PATH
    try:
        if _LOG_FILE_PATH:
            return _LOG_FILE_PATH

        console_dir = Path(config("CONSOLE_OUTPUT_DIRECTORY"))
        console_dir.mkdir(parents=True, exist_ok=True)

        # Use a single prefix 'pubchem' for all modules to ensure a single file
        log_file = console_dir / f"pubchem_{_RUN_TIMESTAMP}.log"

        root_logger = logging.getLogger()
        # If handlers already exist, avoid re-adding them (prevents duplicate logs)
        if not root_logger.handlers:
            fmt = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s', '%m/%d/%Y %H:%M:%S')
            fh = logging.FileHandler(log_file)
            fh.setFormatter(fmt)
            sh = logging.StreamHandler()
            sh.setFormatter(fmt)
            root_logger.setLevel(logging.INFO)
            root_logger.addHandler(fh)
            root_logger.addHandler(sh)

        root_logger.info(f"Logging initialized for {module_name}")
        _LOG_FILE_PATH = str(log_file)
        return _LOG_FILE_PATH

    except Exception as e:
        # Avoid raising here; fallback to stdout only
        print(f"Failed to setup logging: {e}")
        return None
