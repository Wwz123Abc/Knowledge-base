from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

THIRD_PARTY_LOGGERS = (
    "uvicorn",
    "uvicorn.error",
    "uvicorn.access",
    "httpx",
    "httpcore",
    "httpx2",
    "httpcore2",
    "openai",
    "urllib3",
)
QUIET_LOGGERS = ("httpx", "httpcore", "httpx2", "httpcore2", "openai", "urllib3")


_STANDARD_RECORD_FIELDS = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "message",
    "asctime",
}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        # Fields passed as `extra={...}` (e.g. a document_id) were silently dropped before.
        for key, value in record.__dict__.items():
            if key not in _STANDARD_RECORD_FIELDS and key not in payload:
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.INFO)
    for name in THIRD_PARTY_LOGGERS:
        third_party = logging.getLogger(name)
        third_party.handlers = [handler]
        third_party.propagate = False
    for name in QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    _configure_loguru_bridge()


def _configure_loguru_bridge() -> None:
    try:
        from loguru import logger as loguru_logger
    except ImportError:
        return

    def forward(message) -> None:
        record = message.record
        content = str(record["message"])
        if record["exception"]:
            content += f"\n{record['exception']}"
        logging.getLogger(str(record["name"])).log(record["level"].no, content)

    loguru_logger.remove()
    loguru_logger.add(forward, level="WARNING")
