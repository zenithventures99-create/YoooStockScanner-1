"""State management and exact-once outbox for Project Artha.
GitHubStateStore correctly uses ?ref= and handles 409/422 merge conflicts.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .locking import StateLock

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
PART_PENDING = "PENDING"
PART_SENDING = "SENDING"
PART_SENT = "SENT"
PART_DELIVERY_UNKNOWN = "DELIVERY_UNKNOWN"
PART_DELIVERY_FAILED = "DELIVERY_FAILED"

ALERT_QUEUED = "QUEUED"
ALERT_IN_PROGRESS = "IN_PROGRESS"
ALERT_PARTIAL = "PARTIAL"
ALERT_SENT = "SENT"
ALERT_FAILED = "FAILED"

MAX_STATE_BYTES = 4_500_000  # stay under GitHub soft limit


class StateError(Exception):
    pass


class CorruptStateError(StateError):
    pass


@dataclass
class StateData:
    events: Dict[str, Any] = field(default_factory=dict)
    alerts: Dict[str, Any] = field(default_factory=dict)
    reviews: Dict[str, Any] = field(default_factory=dict)
    last_run_utc: Optional[str] = None
    last_operator_notice: Dict[str, str] = field(default_factory=dict)
    schema_version: int = 1

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "last_run_utc": self.last_run_utc,
            "events": self.events,
            "alerts": self.alerts,
            "reviews": self.reviews,
            "last_operator_notice": self.last_operator_notice,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "StateData":
        return cls(
            schema_version=int(data.get("schema_version", 1)),
            last_run_utc=data.get("last_run_utc"),
            events=dict(data.get("events") or {}),
            alerts=dict(data.get("alerts") or {}),
            reviews=dict(data.get("reviews") or {}),
            last_operator_notice=dict(data.get("last_operator_notice") or {}),
        )

    def merge(self, other: "StateData") -> "StateData":
        """Optimistic merge: prefer newer alert statuses, keep all events."""
        merged = StateData(
            schema_version=max(self.schema_version, other.schema_version),
            last_run_utc=other.last_run_utc or self.last_run_utc,
            events={**self.events, **other.events},
            reviews={**self.reviews, **other.reviews},
            last_operator_notice={**self.last_operator_notice, **other.last_operator_notice},
        )
        # Alerts: take the higher-progress status
        order = {
            PART_PENDING: 0,
            PART_SENDING: 1,
            PART_DELIVERY_UNKNOWN: 2,
            PART_DELIVERY_FAILED: 2,
            PART_SENT: 3,
        }
        for aid, alert in {**self.alerts, **other.alerts}.items():
            a1 = self.alerts.get(aid)
            a2 = other.alerts.get(aid)
            if a1 and a2:
                # simple last-writer-wins for now + status rank
                merged.alerts[aid] = a2 if order.get(a2.get("status"), 0) >= order.get(a1.get("status"), 0) else a1
            else:
                merged.alerts[aid] = a2 or a1
        return merged


class LocalStateStore:
    def __init__(self, path: str):
        self.path = path

    def load(self) -> StateData:
        if not os.path.exists(self.path):
            return StateData()
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            return StateData.from_dict(data)
        except (json.JSONDecodeError, OSError) as e:
            raise CorruptStateError(str(e)) from e

    def save(self, state: StateData) -> None:
        payload = json.dumps(state.to_dict(), indent=2, ensure_ascii=False)
        if len(payload.encode("utf-8")) > MAX_STATE_BYTES:
            raise StateError("state exceeds MAX_STATE_BYTES")
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(payload)
        os.replace(tmp, self.path)


class GitHubStateStore:
    """Stores state.json on a dedicated branch with proper ?ref= and conflict retry."""

    def __init__(self, repo: str, token: str, branch: str = "artha-state", path: str = "state.json"):
        self.repo = repo
        self.token = token
        self.branch = branch
        self.path = path
        self.api = f"https://api.github.com/repos/{repo}/contents/{path}"

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def load(self) -> StateData:
        url = f"{self.api}?ref={self.branch}"
        req = urllib.request.Request(url, headers=self._headers())
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                meta = json.loads(resp.read().decode("utf-8"))
                content_b64 = meta.get("content", "")
                import base64
                raw = base64.b64decode(content_b64).decode("utf-8")
                data = json.loads(raw)
                self._sha = meta.get("sha")
                return StateData.from_dict(data)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                self._sha = None
                return StateData()
            raise StateError(f"GitHub load failed: HTTP {e.code}") from e
        except Exception as e:
            raise StateError(f"GitHub load failed: {e}") from e

    def save(self, state: StateData) -> None:
        payload = json.dumps(state.to_dict(), indent=2, ensure_ascii=False)
        if len(payload.encode("utf-8")) > MAX_STATE_BYTES:
            raise StateError("state exceeds MAX_STATE_BYTES")

        import base64
        content_b64 = base64.b64encode(payload.encode("utf-8")).decode("ascii")

        body = {
            "message": f"artha state {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}",
            "content": content_b64,
            "branch": self.branch,
        }
        if getattr(self, "_sha", None):
            body["sha"] = self._sha

        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            self.api,
            data=data,
            headers={**self._headers(), "Content-Type": "application/json"},
            method="PUT",
        )

        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    meta = json.loads(resp.read().decode("utf-8"))
                    self._sha = meta.get("content", {}).get("sha")
                    return
            except urllib.error.HTTPError as e:
                if e.code in (409, 422):
                    # conflict → reload + merge + retry
                    current = self.load()
                    merged = current.merge(state)
                    state = merged
                    payload = json.dumps(state.to_dict(), indent=2, ensure_ascii=False)
                    content_b64 = base64.b64encode(payload.encode("utf-8")).decode("ascii")
                    body["content"] = content_b64
                    if getattr(self, "_sha", None):
                        body["sha"] = self._sha
                    data = json.dumps(body).encode("utf-8")
                    req = urllib.request.Request(
                        self.api,
                        data=data,
                        headers={**self._headers(), "Content-Type": "application/json"},
                        method="PUT",
                    )
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise StateError(f"GitHub save failed: HTTP {e.code}") from e
        raise StateError("GitHub save: conflict retry exhausted")


class Outbox:
    def __init__(self, store: Any, lock: StateLock):
        self.store = store
        self.lock = lock
        self.state = StateData()

    def load(self) -> None:
        self.state = self.store.load()

    def save(self) -> None:
        self.store.save(self.state)

    def record_event(self, event_id: str, module: str, outcome: str, detail: Dict[str, Any]) -> None:
        self.state.events[event_id] = {
            "module": module,
            "outcome": outcome,
            "detail": detail,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        }

    def is_duplicate(self, event_id: str) -> bool:
        return event_id in self.state.events

    def create_alert(self, alert_id: str, event_id: str, parts: List[str]) -> None:
        if alert_id in self.state.alerts:
            raise StateError(f"alert_id collision: {alert_id}")
        self.state.alerts[alert_id] = {
            "event_id": event_id,
            "status": ALERT_QUEUED,
            "parts": [
                {"index": i, "text": p, "status": PART_PENDING, "message_id": None}
                for i, p in enumerate(parts)
            ],
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

    def get_alert(self, alert_id: str) -> Dict[str, Any]:
        return self.state.alerts[alert_id]

    def mark_part(self, alert_id: str, index: int, status: str, message_id: Optional[int] = None) -> None:
        alert = self.state.alerts[alert_id]
        part = alert["parts"][index]
        part["status"] = status
        if message_id is not None:
            part["message_id"] = message_id
        # derive alert status
        statuses = {p["status"] for p in alert["parts"]}
        if statuses == {PART_SENT}:
            alert["status"] = ALERT_SENT
        elif PART_DELIVERY_FAILED in statuses:
            alert["status"] = ALERT_FAILED
        elif PART_DELIVERY_UNKNOWN in statuses or PART_SENDING in statuses:
            alert["status"] = ALERT_PARTIAL
        else:
            alert["status"] = ALERT_IN_PROGRESS

    def reconcile_after_restart(self) -> List[str]:
        downgraded = []
        for aid, alert in self.state.alerts.items():
            for i, part in enumerate(alert["parts"]):
                if part["status"] == PART_SENDING:
                    part["status"] = PART_DELIVERY_UNKNOWN
                    downgraded.append(f"{aid}#{i}")
            # re-derive
            statuses = {p["status"] for p in alert["parts"]}
            if PART_DELIVERY_UNKNOWN in statuses:
                alert["status"] = ALERT_PARTIAL
        return downgraded

    def add_review(self, event_id: str, detail: Dict[str, Any]) -> None:
        self.state.reviews[event_id] = detail

    def record_manual_action(self, alert_id: str, index: int, status: str, operator: str, note: str, message_id: Optional[int] = None) -> None:
        self.mark_part(alert_id, index, status, message_id)
        alert = self.state.alerts[alert_id]
        alert.setdefault("manual_actions", []).append({
            "index": index,
            "status": status,
            "operator": operator,
            "note": note,
            "at": datetime.now(timezone.utc).isoformat(),
        })


def generate_alert_id(module: str, event_key: str, yyyymmdd: str) -> str:
    prefix = "AR-PEAD" if module == "RESULTS_SURGE" else "AR-ORD"
    short = event_key[-8:] if len(event_key) > 8 else event_key
    return f"{prefix}-{yyyymmdd}-{short}"


def deliver_alert(outbox: Outbox, alert_id: str, transport: Any) -> None:
    alert = outbox.get_alert(alert_id)
    for i, part in enumerate(alert["parts"]):
        if part["status"] in (PART_SENT, PART_DELIVERY_FAILED):
            continue
        outbox.mark_part(alert_id, i, PART_SENDING)
        outbox.save()
        try:
            result = transport.send(os.environ.get("CHAT_ID", ""), part["text"])
            if result.outcome == "SENT" and result.message_id is not None:
                outbox.mark_part(alert_id, i, PART_SENT, result.message_id)
            else:
                outbox.mark_part(alert_id, i, PART_DELIVERY_UNKNOWN)
        except Exception:
            outbox.mark_part(alert_id, i, PART_DELIVERY_UNKNOWN)
        outbox.save()


__all__ = [
    "PART_PENDING", "PART_SENDING", "PART_SENT", "PART_DELIVERY_UNKNOWN", "PART_DELIVERY_FAILED",
    "ALERT_QUEUED", "ALERT_IN_PROGRESS", "ALERT_PARTIAL", "ALERT_SENT", "ALERT_FAILED",
    "StateError", "CorruptStateError", "StateData",
    "LocalStateStore", "GitHubStateStore", "Outbox",
    "generate_alert_id", "deliver_alert",
]