"""SMS alert channel using the Fast2SMS "Quick SMS" route.

Why Fast2SMS: Indian provider, plain HTTPS + JSON (no SDK, so nothing extra to
install on the Raspberry Pi), and the Quick route needs no DLT template
registration. Check Fast2SMS's current terms and pricing before real use.

Contract (see alerts/base.py):

    __init__(api_key, api_url, to_number)
        api_key   -- from the Fast2SMS "Dev API" page (sent as a header, never in
                     the URL, so it cannot leak through an exception message).
        api_url   -- FAST2SMS_URL unless you have a reason to change it.
        to_number -- one Indian mobile number, or several separated by commas.
                     "+91 98765 43210", "09876543210" and "9876543210" all work.

    send(message) -> None
        Raises AlertSendError on any failure. Never fails silently.

Inbound/"answering" SMS is a Phase 4+ stretch goal per the brief, so it is
deliberately not built into this class.
"""

from __future__ import annotations

import logging
import re
from typing import Final

import requests

from alerts.base import AlertChannel, AlertSendError

log: Final = logging.getLogger(__name__)

FAST2SMS_URL: Final = "https://www.fast2sms.com/dev/bulkV2"


def normalize_number(raw: str) -> str:
    """Return a bare 10-digit Indian mobile number, or raise ValueError."""
    digits = re.sub(r"[\s\-()]", "", raw)
    digits = digits.removeprefix("+")
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if not re.fullmatch(r"[6-9]\d{9}", digits):
        raise ValueError(f"not a valid Indian mobile number: {raw!r}")
    return digits


def sms_safe(message: str) -> str:
    """Keep the alert to plain ASCII.

    Characters like the em dash and the degree sign force a "Unicode" SMS,
    which holds far fewer characters per segment and may need a different
    provider setting. Plain ASCII keeps the alert in a single segment.
    """
    text = message.replace("—", "-").replace("–", "-").replace("°", "")
    return text.encode("ascii", errors="replace").decode("ascii")


class SmsAlertChannel(AlertChannel):
    def __init__(self, api_key: str, api_url: str, to_number: str) -> None:
        if not api_key:
            raise ValueError("SMS api_key must be a non-empty string")
        if not to_number:
            raise ValueError("SMS to_number must be provided")

        # Validate now, at startup -- not for the first time during a frost event.
        numbers = [normalize_number(n) for n in to_number.split(",") if n.strip()]
        if not numbers:
            raise ValueError("SMS to_number must contain at least one number")

        self._api_key = api_key
        self.api_url = api_url or FAST2SMS_URL
        self.numbers = ",".join(numbers)

    def send(self, message: str) -> None:
        payload = {
            "route": "q",  # Quick SMS: free text, no DLT template
            "message": sms_safe(message),
            "numbers": self.numbers,
        }
        headers = {"Authorization": self._api_key, "Content-Type": "application/json"}

        try:
            resp = requests.post(self.api_url, json=payload, headers=headers, timeout=10)
        except requests.RequestException as exc:
            raise AlertSendError(f"sms request failed: {exc}") from exc

        try:
            body = resp.json()
        except ValueError:
            body = {}

        # Fast2SMS answers {"return": true, ...} on success. Check the body, not
        # just the HTTP status: a rejected send (bad key, empty wallet) can still
        # come back as a normal-looking response.
        if not resp.ok or body.get("return") is not True:
            detail = body.get("message", resp.text[:200])
            raise AlertSendError(f"sms api error {resp.status_code}: {detail}")

        log.info("sms sent to %s (request_id=%s)", self.numbers, body.get("request_id"))
