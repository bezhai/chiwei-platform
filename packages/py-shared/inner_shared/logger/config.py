"""
Logging configuration utilities.
"""

import logging
import logging.config
from pathlib import Path
from typing import Any

from .formatter import CustomJSONFormatter


def create_logging_config(
    log_dir: str = "/logs/app",
    log_file: str = "app.log",
    level: str = "INFO",
    max_bytes: int = 10485760,  # 10MB
    backup_count: int = 5,
    enable_console: bool = True,
    enable_file: bool = True,
) -> dict[str, Any]:
    """
    Create a logging configuration dict.

    Args:
        log_dir: Directory for log files
        log_file: Log file name
        level: Log level
        max_bytes: Max size per log file
        backup_count: Number of backup files to keep
        enable_console: Enable console logging
        enable_file: Enable file logging

    Returns:
        Logging configuration dict
    """
    handlers = []
    handler_configs = {}

    if enable_console:
        handlers.append("console")
        handler_configs["console"] = {
            "class": "logging.StreamHandler",
            "level": level,
            "formatter": "json",
            "stream": "ext://sys.stdout",
        }

    if enable_file:
        handlers.append("file")
        log_path = Path(log_dir)
        log_path.mkdir(parents=True, exist_ok=True)

        handler_configs["file"] = {
            "class": "logging.handlers.RotatingFileHandler",
            "level": level,
            "formatter": "json",
            "filename": str(log_path / log_file),
            "maxBytes": max_bytes,
            "backupCount": backup_count,
            "encoding": "utf-8",
        }

    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "json": {
                "()": CustomJSONFormatter,
                "format": "%(timestamp)s %(level)s %(name)s %(message)s",
            },
        },
        "handlers": handler_configs,
        "root": {
            "level": level,
            "handlers": handlers,
        },
    }


def setup_logging(config: dict[str, Any] | None = None, **kwargs) -> None:
    """
    Setup logging with the given configuration.

    Args:
        config: Optional logging configuration dict
        **kwargs: Arguments passed to create_logging_config if config is None
    """
    if config is None:
        config = create_logging_config(**kwargs)
    logging.config.dictConfig(config)
