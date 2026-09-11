import json
import logging

from app.logging_setup import JsonFormatter, configure_logging


def test_httpx2_logger_uses_json_handler_without_propagation():
    configure_logging()

    logger = logging.getLogger("httpx2")
    assert logger.propagate is False
    assert len(logger.handlers) == 1
    assert isinstance(logger.handlers[0].formatter, JsonFormatter)
    record = logging.LogRecord("httpx2", logging.WARNING, __file__, 1, "request failed", (), None)
    payload = json.loads(logger.handlers[0].formatter.format(record))
    assert payload["logger"] == "httpx2"
    assert payload["message"] == "request failed"


def test_loguru_messages_are_forwarded_to_json(capsys):
    from loguru import logger

    configure_logging()
    logger.warning("fastembed-style-warning")

    output = capsys.readouterr().err.strip().splitlines()
    payload = json.loads(output[-1])
    assert payload["message"] == "fastembed-style-warning"
    assert payload["level"] == "WARNING"
