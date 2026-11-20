import pytest
import requests

from alerts.base import AlertSendError
from alerts.dispatcher import build_channels
from alerts.twilio_sms import TwilioSmsChannel


class FakeResponse:
    def __init__(self, status_code=201, body=None, text=""):
        self.status_code = status_code
        self._body = body
        self.text = text or str(body)

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


def _channel(to="9876543210") -> TwilioSmsChannel:
    return TwilioSmsChannel("ACfake", "token-secret", "+15551234567", to)


# ---- construction ----------------------------------------------------------


def test_numbers_are_normalised_to_e164():
    ch = _channel("98765 43210, 09123456780")
    assert ch.recipients == ["+919876543210", "+919123456780"]


@pytest.mark.parametrize("bad_from", ["15551234567", "+0123", "abc", ""])
def test_bad_from_number_fails_at_startup(bad_from):
    with pytest.raises(ValueError):
        TwilioSmsChannel("ACfake", "tok", bad_from, "9876543210")


def test_bad_to_number_fails_at_startup():
    with pytest.raises(ValueError):
        _channel("12345")


def test_missing_credentials_fail_at_startup():
    with pytest.raises(ValueError):
        TwilioSmsChannel("", "tok", "+15551234567", "9876543210")


# ---- send ------------------------------------------------------------------


def test_send_posts_expected_request(monkeypatch):
    seen = {}

    def fake_post(url, data=None, auth=None, timeout=None):
        seen.update(url=url, data=data, auth=auth, timeout=timeout)
        return FakeResponse(201, {"sid": "SM123", "status": "queued"})

    monkeypatch.setattr(requests, "post", fake_post)
    _channel().send("FROST — min -2.5°C")

    assert seen["url"] == "https://api.twilio.com/2010-04-01/Accounts/ACfake/Messages.json"
    assert seen["data"] == {
        "To": "+919876543210",
        "From": "+15551234567",
        "Body": "FROST - min -2.5C",  # ASCII-safe
    }
    assert seen["auth"] == ("ACfake", "token-secret")  # Basic auth, not in URL/body
    assert "token-secret" not in seen["url"]
    assert "token-secret" not in str(seen["data"])
    assert seen["timeout"] == 10


def test_trial_unverified_number_error_is_reported(monkeypatch):
    body = {"code": 21608, "message": "The number is unverified.", "status": 400}
    monkeypatch.setattr(requests, "post", lambda *a, **k: FakeResponse(400, body))
    with pytest.raises(AlertSendError, match="21608"):
        _channel().send("x")


def test_non_json_error_still_raises(monkeypatch):
    monkeypatch.setattr(requests, "post", lambda *a, **k: FakeResponse(503, None, "bad gateway"))
    with pytest.raises(AlertSendError, match="503"):
        _channel().send("x")


def test_network_error_raises_and_does_not_leak_token(monkeypatch):
    def boom(*a, **k):
        raise requests.ConnectionError("no network")

    monkeypatch.setattr(requests, "post", boom)
    with pytest.raises(AlertSendError) as exc:
        _channel().send("x")
    assert "token-secret" not in str(exc.value)


def test_one_failing_recipient_does_not_block_the_others(monkeypatch):
    sent = []

    def fake_post(url, data=None, auth=None, timeout=None):
        if data["To"] == "+919111111111":
            return FakeResponse(400, {"code": 21608, "message": "unverified"})
        sent.append(data["To"])
        return FakeResponse(201, {"sid": "SM1"})

    monkeypatch.setattr(requests, "post", fake_post)
    ch = _channel("9111111111,9222222222")
    with pytest.raises(AlertSendError, match="9111111111"):
        ch.send("x")
    assert sent == ["+919222222222"]  # the good recipient still got the alert


# ---- wiring via build_channels ---------------------------------------------

_ENV = {
    "TWILIO_ACCOUNT_SID": "ACenv",
    "TWILIO_AUTH_TOKEN": "env-token",
    "TWILIO_FROM_NUMBER": "+15551234567",
    "TWILIO_TO_NUMBER": "9876543210",
}


def test_twilio_from_env(monkeypatch):
    for k, v in _ENV.items():
        monkeypatch.setenv(k, v)
    channels = build_channels({"alerts": {"channels": ["twilio"]}, "twilio": {}})
    assert isinstance(channels["twilio"], TwilioSmsChannel)
    assert channels["twilio"].recipients == ["+919876543210"]


def test_replace_me_placeholders_fall_back_to_env(monkeypatch):
    for k, v in _ENV.items():
        monkeypatch.setenv(k, v)
    cfg = {"alerts": {"channels": ["twilio"]}, "twilio": {"account_sid": "REPLACE_ME"}}
    assert build_channels(cfg)["twilio"].url.endswith("/ACenv/Messages.json")


def test_twilio_missing_credentials_raises(monkeypatch):
    for k in _ENV:
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(ValueError):
        build_channels({"alerts": {"channels": ["twilio"]}})