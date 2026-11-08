"""Slack alerts through an incoming webhook (SLACK_WEBHOOK_URL in .env). Silent if not configured or unreachable."""
from __future__ import annotations

import json
import urllib.request

from . import config


def slack(text: str) -> None:
    if not config.SLACK_WEBHOOK_URL:
        return
    try:
        req = urllib.request.Request(config.SLACK_WEBHOOK_URL, data=json.dumps({"text": text}).encode(),
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10).read()
    except Exception as e:  # an alert failure must never stop the engine
        print(f"(slack alert failed: {e})", flush=True)
