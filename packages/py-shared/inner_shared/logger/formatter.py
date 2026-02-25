"""
Custom JSON log formatter.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from pythonjsonlogger import jsonlogger

from ..middlewares import get_trace_id


class CustomJSONFormatter(jsonlogger.JsonFormatter):
    """
    Custom JSON formatter.

    Features:
    1. Rename levelname field to level
    2. Convert log level to lowercase (INFO -> info)
    3. Consistent with main-server log format
    """

    def add_fields(
        self,
        log_record: dict[str, Any],
        record: logging.LogRecord,
        message_dict: dict[str, Any],
    ) -> None:
        """Add fields to log record."""
        super().add_fields(log_record, record, message_dict)

        # Rename levelname to level and convert to lowercase
        if "levelname" in log_record:
            log_record["level"] = log_record["levelname"].lower()
            del log_record["levelname"]
        elif hasattr(record, "levelname"):
            log_record["level"] = record.levelname.lower()

        # Add ISO8601 timestamp (UTC+8)
        if hasattr(record, "created"):
            tz = timezone(timedelta(hours=8))
            dt = datetime.fromtimestamp(record.created, tz)
            log_record["timestamp"] = dt.isoformat()

        # Add logger name
        if "name" not in log_record and hasattr(record, "name"):
            log_record["name"] = record.name

        # Ensure message field exists
        if "message" not in log_record and hasattr(record, "message"):
            log_record["message"] = record.message

        # Add traceId field
        trace_id = get_trace_id()
        if trace_id:
            log_record["traceId"] = trace_id
