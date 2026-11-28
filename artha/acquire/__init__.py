"""Acquisition layer for Project Artha.
Only adapters that actually work from GitHub-hosted runners are enabled.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List

from .base import AcquisitionResult
from . import nse, bse

__all__ = ["acquire_events", "AcquisitionResult"]


def acquire_events(now: datetime, state: Any) -> AcquisitionResult:
    """Main entry point called by scanner in LIVE mode."""
    events: List[dict] = []
    source_status: Dict[str, Dict[str, Any]] = {}

    # Run adapters independently – one failure must not stop others
    for name, adapter_fn in [
        ("nse", nse.fetch_announcements),
        ("bse", bse.fetch_announcements),
    ]:
        try:
            result = adapter_fn(now, state)
            events.extend(result.get("events", []))
            source_status[name] = {
                "ok": True,
                "detail": result.get("detail", "ok"),
                "fetched": result.get("fetched", 0),
            }
        except Exception as e:
            source_status[name] = {
                "ok": False,
                "detail": f"{type(e).__name__}: {str(e)[:200]}",
                "fetched": 0,
            }

    return AcquisitionResult(events=events, source_status=source_status)