"""Structured logging: one JSON object per line (when asked for), tagged with the request id."""

from __future__ import annotations

import contextvars
import json
import logging
import sys
from datetime import UTC, datetime

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="")
_STANDARD = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime", "taskName"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if rid := request_id_var.get():
            entry["request_id"] = rid
        entry.update({k: v for k, v in record.__dict__.items() if k not in _STANDARD})
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


def configure_logging(fmt: str = "text", level: str = "INFO") -> None:
    """Root handler on stderr. Safe to call again (replaces the handler it installed before)."""
    handler = logging.StreamHandler(sys.stderr)
    handler.set_name("hive")
    if fmt == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root = logging.getLogger()
    root.handlers = [h for h in root.handlers if h.get_name() != "hive"] + [handler]
    root.setLevel(level.upper())
