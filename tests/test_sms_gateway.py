import pytest
import requests

from alerts.base import AlertSendError
from alerts.dispatcher import build_channels
from alerts.sms_gateway import FAST2SMS_URL, SmsAlertChannel, normalize_number, sms_safe


class FakeResponse:
    def __init__(self, status_code=200, body=None, text=""):
        self.status_code = status_code
        self._body = body
        self.text = text or str(body)
        self.ok = status_code < 400

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


def _channel() -> SmsAlertChannel:
    return SmsAlertChannel("secret-key", FAST2SMS_URL, "9876543210")


# ---- number handling -------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    ["9876543210", "+91 98765 43210", "919876543210", "09876543210", "98765-43210"],
)
def test_normalize_number_accepts_common_formats(raw):
    assert normalize_number(raw) == "9876543210"


@pytest.mark.parametrize("raw", ["12345", "5876543210", "98765432101234", "abcdefghij", ""])
def test_normalize_number_rejects_bad_numbers(raw):
    with pytest.raises(ValueError):
        normalize_number(raw)


def test_bad_number_fails_at_startup_not_at_send_time():
    with pytest.raises(ValueError):
        SmsAlertChannel("key", FAST2SMS_URL, "not-a-number")


def test_multiple_numbers_are_comma_joined():
    ch = SmsAlertChannel("key", FAST2SMS_URL, "9876543210, +91 91234 56780")
    assert ch.numbers == "9876543210,9123456780"


# ---- message text ----------------------------------------------------------


def test_sms_safe_removes_non_ascii():
    out = sms_safe("FROST RISK ALERT — predicted min -2.5°C")
    assert out == "FROST RISK ALERT - predicted min -2.5C"
    assert out.isascii()


# ---- send ------------------------------------------------------------------


def test_send_posts_expected_request(monkeypatch):
    seen = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        seen.update(url=url, json=json, headers=headers, timeout=timeout)
        return FakeResponse(200, {"return": True, "request_id": "abc", "message": ["ok"]})

    monkeypatch.setattr(requests, "post", fake_post)
    _channel().send("hello — world")

    assert seen["url"] == FAST2SMS_URL
    assert seen["json"] == {"route": "q", "message": "hello - world", "numbers": "9876543210"}
    assert seen["headers"]["Authorization"] == "secret-key"
    assert "secret-key" not in seen["url"]  # key must never travel in the URL
    assert seen["timeout"] == 10


def test_api_rejection_in_body_raises_even_on_http_200(monkeypatch):
    monkeypatch.setattr(
        requests,
        "post",
        lambda *a, **k: FakeResponse(200, {"return": False, "message": "Invalid Authentication"}),
    )
    with pytest.raises(AlertSendError, match="Invalid Authentication"):
        _channel().send("x")


def test_http_error_raises(monkeypatch):
    monkeypatch.setattr(
        requests, "post", lambda *a, **k: FakeResponse(500, None, text="server exploded")
    )
    with pytest.raises(AlertSendError, match="500"):
        _channel().send("x")


def test_network_error_raises_and_does_not_leak_key(monkeypatch):
    def boom(*a, **k):
        raise requests.ConnectionError("no network")

    monkeypatch.setattr(requests, "post", boom)
    with pytest.raises(AlertSendError) as exc:
        _channel().send("x")
    assert "secret-key" not in str(exc.value)


# ---- wiring via build_channels ---------------------------------------------


def test_sms_from_env(monkeypatch):
    monkeypatch.setenv("FAST2SMS_API_KEY", "env-key")
    monkeypatch.setenv("SMS_TO_NUMBER", "9876543210")
    channels = build_channels({"alerts": {"channels": ["sms"]}, "sms": {}})
    assert isinstance(channels["sms"], SmsAlertChannel)
    assert channels["sms"].numbers == "9876543210"


def test_replace_me_placeholders_fall_back_to_env(monkeypatch):
    monkeypatch.setenv("FAST2SMS_API_KEY", "env-key")
    monkeypatch.setenv("SMS_TO_NUMBER", "9876543210")
    cfg = {
        "alerts": {"channels": ["sms"]},
        "sms": {"api_key": "REPLACE_ME", "api_url": "REPLACE_ME", "to_number": "REPLACE_ME"},
    }
    ch = build_channels(cfg)["sms"]
    assert ch.api_url == FAST2SMS_URL
    assert ch.numbers == "9876543210"


def test_sms_missing_credentials_raises(monkeypatch):
    monkeypatch.delenv("FAST2SMS_API_KEY", raising=False)
    monkeypatch.delenv("SMS_TO_NUMBER", raising=False)
    with pytest.raises(ValueError):
        build_channels({"alerts": {"channels": ["sms"]}})