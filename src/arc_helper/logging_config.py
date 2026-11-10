"""
Logging configuration for ARLO.
"""

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path


def setup_logging(app_dir: Path, *, debug_mode: bool = False) -> logging.Logger:
    """
    Configure and return the application logger.

    Call this once at application startup.
    """
    logger = logging.getLogger("arc_helper")

    if debug_mode:
        logger.setLevel(logging.DEBUG)
    else:
        logger.setLevel(logging.INFO)

    if logger.handlers:
        return logger

    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.DEBUG)
    console_format = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"
    )
    console_handler.setFormatter(console_format)
    logger.addHandler(console_handler)

    # File handler, always: launched from Steam or a shortcut there is no
    # console, and connection failures should still be readable afterwards.
    # Without debug mode it gets INFO and up; size-capped by rotation.
    log_file = app_dir / "arc_helper.log"
    try:
        file_handler = RotatingFileHandler(
            log_file, maxBytes=5 * 1024 * 1024, backupCount=2, encoding="utf-8"
        )
    except OSError as e:  # read-only install folder: console only
        logger.warning("Cannot write log file %s: %s", log_file, e)
        return logger
    file_handler.setLevel(logging.DEBUG)
    file_format = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_handler.setFormatter(file_format)
    logger.addHandler(file_handler)

    return logger
