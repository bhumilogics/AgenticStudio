"""structlog setup: structured (JSON or console) logs to stdout + a file."""

from __future__ import annotations

import logging
import os
import sys

import structlog

from core.config import settings


def configure_logging() -> None:
    log_dir = os.environ.get("AGENTICSTUDIO_LOG_DIR", "./data/logs")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, "agenticstudio.log")

    logging.basicConfig(
        format="%(message)s",
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_path),
        ],
    )

    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
    ]

    renderer = structlog.processors.JSONRenderer() if settings.log_json else structlog.dev.ConsoleRenderer()

    structlog.configure(
        processors=[*shared_processors, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(processor=renderer, foreign_pre_chain=shared_processors)
    for handler in logging.getLogger().handlers:
        handler.setFormatter(formatter)

    # The terminal gets the readable live console (observability/tracer.py console_line); the JSON
    # "graph_event" line for the same event would just duplicate it there, so keep it to the file.
    if os.environ.get("AGENTICSTUDIO_LIVE_CONSOLE", "1").strip().lower() not in {"0", "false", "no", "off"}:
        class _SkipGraphEvents(logging.Filter):
            def filter(self, record: logging.LogRecord) -> bool:
                return not (isinstance(record.msg, dict) and record.msg.get("event") == "graph_event")

        for handler in logging.getLogger().handlers:
            if type(handler) is logging.StreamHandler:  # the stdout one, not the FileHandler (a subclass)
                handler.addFilter(_SkipGraphEvents())


def get_logger(name: str = "agenticstudio"):
    return structlog.get_logger(name)
