"""Structured contextual logging with rotation and secret-safe defaults."""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path


class ContextFilter(logging.Filter):
    """Ensure correlation fields exist for every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "turn_id"):
            record.turn_id = "-"
        if not hasattr(record, "event_id"):
            record.event_id = "-"
        return True


def configure_logging(log_dir: Path, level: str = "INFO", debug: bool = False) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.DEBUG if debug else getattr(logging, level.upper(), logging.INFO))

    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)s %(name)s turn=%(turn_id)s event=%(event_id)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    context_filter = ContextFilter()

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    console.addFilter(context_filter)
    root.addHandler(console)

    rotating = logging.handlers.RotatingFileHandler(
        log_dir / "bmo.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    rotating.setFormatter(formatter)
    rotating.addFilter(context_filter)
    root.addHandler(rotating)
