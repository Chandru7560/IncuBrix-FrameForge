from __future__ import annotations

import json
import logging

from scenepilot.observability import JsonLogFormatter


def test_json_log_includes_required_correlation_and_job_fields():
    record = logging.LogRecord(
        "scenepilot.test",
        logging.INFO,
        __file__,
        12,
        "job_started",
        (),
        None,
    )
    record.job_id = "job-123"
    record.correlation_id = "request-456"

    payload = json.loads(JsonLogFormatter().format(record))
    assert payload["timestamp"]
    assert payload["level"] == "INFO"
    assert payload["severity"] == "INFO"
    assert payload["job_id"] == "job-123"
    assert payload["X-Correlation-Id"] == "request-456"
