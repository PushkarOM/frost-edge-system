"""SMS alert channel using Twilio's REST API.

Plain HTTPS via `requests` (same as the Telegram and Fast2SMS channels), so there
is no extra SDK to install on the Raspberry Pi.

Trial-account facts that matter for the demo (check Twilio's docs, they change):
  * A trial account can only text numbers you have VERIFIED in the Twilio
    console (Phone Numbers > Verified Caller IDs). The number you signed up
    with is verified automatically. Anything else fails with error 21608.
  * Trial messages start with "Sent from a Twilio trial account".
  * HTTP 201 means Twilio ACCEPTED the message, not that the phone received it.
    Check delivery in the console under Monitor > Logs > Messaging.
  * SMS to India is one-way, and the sender shown on the phone is not your
    Twilio number.

Contract (see alerts/base.py): send(message) raises AlertSendError on failure.
"""

from __future__ import annotations

import logging
import re
from typing import Final

import requests

from alerts.base import AlertChannel, AlertSendError
from alerts.sms_gateway import normalize_number, sms_safe

log: Final = logging.getLogger(__name__)

TWILIO_API: Final = "https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"


def _e164_india(raw: str) -> str:
    """9876543210 / +91 98765 43210 / 09876543210 -> +919876543210."""
    return "+91" + normalize_number(raw)


class TwilioSmsChannel(AlertChannel):
    def __init__(self, account_sid: str, auth_token: str, from_number: str, to_number: str) -> None:
        if not account_sid or not auth_token:
            raise ValueError("Twilio account_sid and auth_token must be non-empty")
        if not re.fullmatch(r"\+[1-9]\d{6,14}", from_number.replace(" ", "")):
            raise ValueError(
                f"Twilio from_number must be in +E.164 form like +15551234567, got {from_number!r}"
            )

        # Validate recipients now, not for the first time during a frost event.
        recipients = [_e164_india(n) for n in to_number.split(",") if n.strip()]
        if not recipients:
            raise ValueError("Twilio to_number must contain at least one number")

        self._auth = (account_sid, auth_token)  # sent as a Basic-auth header
        self.url = TWILIO_API.format(sid=account_sid)
        self.from_number = from_number.replace(" ", "")
        self.recipients = recipients

    def _send_one(self, to: str, body: str) -> None:
        try:
            resp = requests.post(
                self.url,
                data={"To": to, "From": self.from_number, "Body": body},
                auth=self._auth,
                timeout=10,
            )
        except requests.RequestException as exc:
            raise AlertSendError(f"twilio request failed: {exc}") from exc

        try:
            payload = resp.json()
        except ValueError:
            payload = {}

        if resp.status_code != 201:
            # Twilio errors look like {"code": 21608, "message": "...", "status": 400}
            code = payload.get("code")
            detail = payload.get("message", resp.text[:200])
            raise AlertSendError(f"twilio error {resp.status_code} (code {code}): {detail}")

        log.info("twilio accepted sms to %s (sid=%s)", to, payload.get("sid"))

    def send(self, message: str) -> None:
        body = sms_safe(message)
        failures: list[str] = []
        for to in self.recipients:  # one API call per recipient; don't stop at the first failure
            try:
                self._send_one(to, body)
            except AlertSendError as exc:
                failures.append(f"{to}: {exc}")
        if failures:
            raise AlertSendError("; ".join(failures))