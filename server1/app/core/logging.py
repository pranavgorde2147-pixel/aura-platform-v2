"""Logging configuration for the AURA backend foundation."""

from __future__ import annotations

import logging
import sys
from typing import Any


def configure_logging() -> None:
    """Configure the root logger for development and production usage."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
        force=True,
    )


def get_logger(name: str) -> logging.Logger:
    """Return a named logger instance."""
    return logging.getLogger(name)
