"""PII must not reach a log line, and this is the test that keeps it true."""

import structlog
from mmp_core.logging import REDACTED_KEYS, configure_logging, get_logger


def _capture(**kwargs):
    configure_logging(service="test", level="debug", json_output=True)
    cap = structlog.testing.LogCapture()
    structlog.configure(processors=[*structlog.get_config()["processors"][:-1], cap])
    get_logger("t").info("event", **kwargs)
    return cap.entries[0]


def test_sensitive_keys_are_masked():
    entry = _capture(api_key="live_abc123", ip="203.0.113.9", user_id="u_1")
    assert entry["api_key"] == "[redacted]"
    assert entry["ip"] == "[redacted]"
    assert entry["user_id"] == "u_1", "non-sensitive fields must survive"


def test_redaction_is_case_insensitive():
    entry = _capture(Authorization="Bearer x", GAID="abc")
    assert entry["Authorization"] == "[redacted]"
    assert entry["GAID"] == "[redacted]"


def test_expected_keys_are_covered():
    """A guard against someone trimming the list in a hurry."""
    required = {"api_key", "password", "secret", "token", "ip", "gaid", "idfa", "email"}
    assert required <= REDACTED_KEYS
