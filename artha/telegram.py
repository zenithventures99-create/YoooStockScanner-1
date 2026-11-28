"""Telegram transport for Project Artha.
Fail-closed. Never stores FakeTransport results as real deliveries.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Outcomes
# ---------------------------------------------------------------------------
OUTCOME_SENT = "SENT"
OUTCOME_UNKNOWN = "DELIVERY_UNKNOWN"
OUTCOME_FAILED = "DELIVERY_FAILED"
OUTCOME_TRANSPORT_UNAVAILABLE = "TRANSPORT_UNAVAILABLE"


class TelegramError(Exception):
    """Raised when live transport is requested but credentials are missing."""


@dataclass(frozen=True)
class SendResult:
    outcome: str
    message_id: Optional[int] = None
    detail: str = ""


class FakeTransport:
    """Only used when ARTHA_FAKE_TRANSPORT=1 is explicitly set."""

    def __init__(self, results: Optional[List[Any]] = None):
        self.sent: List[str] = []
        self._results = results or []

    def send(self, chat_id: str, text: str) -> SendResult:
        self.sent.append(text)
        if self._results:
            item = self._results.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return SendResult(outcome=OUTCOME_SENT, message_id=999999)


class LiveTelegramTransport:
    """Real Telegram Bot API transport."""

    def __init__(self, bot_token: str, timeout: float = 30.0):
        self.bot_token = bot_token
        self.timeout = timeout
        self.base = f"https://api.telegram.org/bot{bot_token}"

    def send(self, chat_id: str, text: str) -> SendResult:
        url = f"{self.base}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
                if body.get("ok") and "result" in body:
                    msg_id = body["result"].get("message_id")
                    if msg_id is None:
                        return SendResult(outcome=OUTCOME_UNKNOWN, detail="missing message_id")
                    return SendResult(outcome=OUTCOME_SENT, message_id=int(msg_id))
                return SendResult(outcome=OUTCOME_FAILED, detail=str(body)[:300])
        except urllib.error.HTTPError as e:
            code = e.code
            if code == 429:
                return SendResult(outcome=OUTCOME_UNKNOWN, detail="429 rate limit")
            if code in (400, 401, 403, 404):
                return SendResult(outcome=OUTCOME_FAILED, detail=f"HTTP {code}")
            return SendResult(outcome=OUTCOME_UNKNOWN, detail=f"HTTP {code}")
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            return SendResult(outcome=OUTCOME_UNKNOWN, detail=str(e)[:200])
        except Exception as e:
            return SendResult(outcome=OUTCOME_UNKNOWN, detail=f"unexpected: {type(e).__name__}")


def get_transport(live: bool = False) -> Any:
    """
    live=True  → real transport or raise TelegramError
    live=False → FakeTransport only if ARTHA_FAKE_TRANSPORT=1, else raise
    """
    bot_token = os.environ.get("BOT_TOKEN", "").strip()
    chat_id = os.environ.get("CHAT_ID", "").strip()

    if live:
        if not bot_token or not chat_id:
            raise TelegramError("BOT_TOKEN and CHAT_ID are required for live delivery")
        return LiveTelegramTransport(bot_token)

    # dry-run / test path
    if os.environ.get("ARTHA_FAKE_TRANSPORT", "").strip() == "1":
        return FakeTransport()
    raise TelegramError(
        "FakeTransport is only allowed when ARTHA_FAKE_TRANSPORT=1 is explicitly set"
    )


__all__ = [
    "OUTCOME_SENT",
    "OUTCOME_UNKNOWN",
    "OUTCOME_FAILED",
    "OUTCOME_TRANSPORT_UNAVAILABLE",
    "TelegramError",
    "SendResult",
    "FakeTransport",
    "LiveTelegramTransport",
    "get_transport",
]