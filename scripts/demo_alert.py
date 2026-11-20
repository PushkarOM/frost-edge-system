"""Demo: fire a fake frost alert through the REAL alert pipeline.

The model (Repo A) isn't plugged in and we can't make frost on demand,
so this script stands in for the model only. It writes a high-risk row
into `predictions`, then runs the same code the alerts service runs:

    predictions row -> AlertPolicy -> dispatcher -> Telegram -> alerts_sent

Run from the repo root:

    uv run python -m scripts.demo_alert
    uv run python -m scripts.demo_alert --score 0.95 --min-temp -4.0
    uv run python -m scripts.demo_alert --twilio     # also text the phone via Twilio
    uv run python -m scripts.demo_alert --sms        # same, via Fast2SMS

Needs config/config.yaml and TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID in .env.
For --twilio also set TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM_NUMBER,
TWILIO_TO_NUMBER in .env. (For --sms: FAST2SMS_API_KEY, SMS_TO_NUMBER.)
"""

import argparse
import sys
from datetime import UTC, datetime

from alerts.alert_policy import AlertPolicy
from alerts.dispatcher import build_channels, evaluate_and_dispatch
from config.loader import load_config
from storage.db import get_connection, init_db, latest_prediction

DEMO_MODEL_VERSION = "demo-v0"  # obvious in the message and on the dashboard


def insert_demo_prediction(db_path: str, score: float, min_temp: float) -> None:
    """Stand-in for what inference/ will write once the model is connected."""
    ts = datetime.now(UTC).isoformat()
    with get_connection(db_path) as conn:
        conn.execute(
            "INSERT INTO predictions (ts, predicted_min_temp_c, risk_score, model_version) "
            "VALUES (?, ?, ?, ?)",
            (ts, min_temp, score, DEMO_MODEL_VERSION),
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Fire a demo frost alert via Telegram.")
    parser.add_argument("--score", type=float, default=0.87, help="fake risk score, 0.0-1.0")
    parser.add_argument("--min-temp", type=float, default=-2.5, help="fake predicted min temp, C")
    parser.add_argument(
        "--sms", action="store_true", help="also send an SMS (needs Fast2SMS in .env)"
    )
    parser.add_argument("--twilio", action="store_true", help="also send an SMS via Twilio")
    parser.add_argument("--config", default=None, help="path to config.yaml (optional)")
    args = parser.parse_args()

    config = load_config(args.config) if args.config else load_config()
    db_path = config["storage"]["db_path"]
    alert_cfg = config["alerts"]
    init_db(db_path)

    # Demo always goes to console + telegram (+ sms with --sms), whatever config.yaml says.
    names = ["console", "telegram"]
    if args.sms:
        names.append("sms")
    if args.twilio:
        names.append("twilio")
    demo_config = {**config, "alerts": {**alert_cfg, "channels": names}}
    channels = build_channels(demo_config)

    # Fresh policy each run, so the first high score always counts as a new crossing.
    policy = AlertPolicy(
        risk_threshold=alert_cfg["risk_threshold"],
        hysteresis_margin=alert_cfg["hysteresis_margin"],
        cooldown_minutes=alert_cfg["cooldown_minutes"],
    )

    insert_demo_prediction(db_path, args.score, args.min_temp)
    prediction = latest_prediction(db_path)  # read it back, like the real service does
    decision = evaluate_and_dispatch(prediction, policy, channels, db_path)

    if decision is None or not decision.should_send:
        print(f"\nNo alert sent: {decision.reason if decision else 'no prediction'}")
        print(f"(score {args.score} vs threshold {alert_cfg['risk_threshold']})")
        return 1

    # The dispatcher swallows channel errors by design, so check what it recorded.
    with get_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT channel, status FROM alerts_sent ORDER BY id DESC LIMIT ?",
            (len(channels),),
        ).fetchall()
    print("\nDelivery results:")
    for channel, status in reversed(rows):
        print(f"  {channel:10s} {status}")

    return 0 if all(status == "sent" for _, status in rows) else 2


if __name__ == "__main__":
    sys.exit(main())