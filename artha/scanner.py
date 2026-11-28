"""Master orchestrator for Project Artha.
Supports FIXTURE and LIVE modes. Fail-closed. Exact module order.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import policy, reports, sources
from .engines import results_surge, big_orders
from .locking import StateLock, LockUnavailable
from .state import (
    LocalStateStore,
    GitHubStateStore,
    Outbox,
    generate_alert_id,
    deliver_alert,
    StateError,
    CorruptStateError,
)
from .telegram import get_transport, TelegramError, OUTCOME_TRANSPORT_UNAVAILABLE

IST = timezone(timedelta(hours=5, minutes=30))

MODULE_RS = "RESULTS_SURGE"
MODULE_BO = "BIG_ORDERS"

QUALIFIED = "QUALIFIED"
REVIEW_REQUIRED = "REVIEW_REQUIRED"
REJECTED = "REJECTED"
NOT_EVALUABLE = "NOT_EVALUABLE"
POLICY_BLOCKED = "POLICY_BLOCKED"

ACQUISITION_NOT_IMPLEMENTED = "ACQUISITION_NOT_IMPLEMENTED"
ACQUISITION_ACTIVE = "ACQUISITION_ACTIVE"
ACQUISITION_UNAVAILABLE = "ACQUISITION_UNAVAILABLE"


def _now_ist() -> datetime:
    return datetime.now(IST)


def load_fixtures(path: Optional[str]) -> List[dict]:
    if not path:
        return []
    p = Path(path)
    if not p.is_file():
        return []
    try:
        with p.open("r", encoding="utf-8") as fh:
            doc = json.load(fh)
        events = doc.get("events", [])
        return [e for e in events if isinstance(e, dict)]
    except Exception:
        return []


def run(
    policy_path: str,
    state_path: str,
    fixtures_path: Optional[str] = None,
    dry_run: bool = True,
    mode: str = "FIXTURE",
) -> Tuple[int, Dict[str, Any]]:
    """
    mode = "FIXTURE" | "LIVE"
    Returns (exit_code, run_report)
    """
    report: Dict[str, Any] = {
        "mode": mode,
        "dry_run": dry_run,
        "operational_scan": False,
        "acquisition_status": ACQUISITION_NOT_IMPLEMENTED,
        "started_at": _now_ist().isoformat(),
        "events_processed": 0,
        "qualified": 0,
        "alerts_sent": 0,
        "failures": [],
        "source_status": {},
    }

    # ---- Policy ----
    try:
        pol = policy.load_policy(policy_path)
    except Exception as e:
        report["failures"].append(f"policy_load: {e}")
        return 1, report

    if mode == "LIVE" and not dry_run and not policy.production_eligible(pol):
        report["failures"].append("POLICY_NOT_PRODUCTION")
        report["acquisition_status"] = "POLICY_BLOCKED"
        return 1, report

    # ---- State ----
    lock_path = state_path + ".lock"
    try:
        with StateLock(lock_path) as guard:
            # Prefer GitHubStateStore when running in Actions
            if os.environ.get("GITHUB_REPOSITORY") and os.environ.get("GITHUB_TOKEN"):
                store = GitHubStateStore(
                    repo=os.environ["GITHUB_REPOSITORY"],
                    token=os.environ["GITHUB_TOKEN"],
                    branch=os.environ.get("STATE_BRANCH", "artha-state"),
                    path="state.json",
                )
            else:
                store = LocalStateStore(state_path)

            outbox = Outbox(store, guard)
            outbox.load()

            # Heartbeat
            outbox.state.last_run_utc = datetime.now(timezone.utc).isoformat()

            events: List[dict] = []

            if mode == "FIXTURE":
                events = load_fixtures(fixtures_path)
                report["acquisition_status"] = ACQUISITION_NOT_IMPLEMENTED
                report["operational_scan"] = False
            else:
                # LIVE mode
                try:
                    from .acquire import acquire_events
                    result = acquire_events(_now_ist(), outbox.state)
                    events = result.events
                    report["source_status"] = result.source_status
                    report["acquisition_status"] = ACQUISITION_ACTIVE
                    report["operational_scan"] = True
                except ImportError:
                    report["acquisition_status"] = ACQUISITION_UNAVAILABLE
                    report["failures"].append("ACQUISITION_UNAVAILABLE")
                    outbox.save()
                    return 1, report
                except Exception as e:
                    report["acquisition_status"] = "ACQUISITION_FAILED"
                    report["failures"].append(f"acquisition: {e}")
                    outbox.save()
                    return 1, report

            # ---- Evaluate ----
            for event in events:
                report["events_processed"] += 1
                module = event.get("module")
                event_id = event.get("event_id") or generate_alert_id(module or "UNK", "x", _now_ist().strftime("%Y%m%d"))

                if module == MODULE_RS:
                    outcome = results_surge.evaluate_results_surge(
                        event, pol, outbox, event_id, sources.IDENTITY_VERIFIED
                    )
                elif module == MODULE_BO:
                    outcome = big_orders.evaluate_big_orders(
                        event, pol, outbox, event_id, sources.IDENTITY_VERIFIED
                    )
                else:
                    continue

                if outcome == QUALIFIED:
                    report["qualified"] += 1

            # ---- Delivery ----
            if not dry_run and report["operational_scan"]:
                try:
                    transport = get_transport(live=True)
                except TelegramError:
                    report["failures"].append(OUTCOME_TRANSPORT_UNAVAILABLE)
                    # leave alerts queued
                else:
                    # Deliver in strict order: all RS first, then all BO
                    for alert_id, alert in list(outbox.state.alerts.items()):
                        if alert.get("status") in ("QUEUED", "IN_PROGRESS", "PARTIAL"):
                            try:
                                deliver_alert(outbox, alert_id, transport)
                                report["alerts_sent"] += 1
                            except Exception as e:
                                report["failures"].append(f"delivery {alert_id}: {e}")

            outbox.save()

    except LockUnavailable:
        report["failures"].append("LOCK_UNAVAILABLE")
        return 3, report
    except CorruptStateError as e:
        report["failures"].append(f"CORRUPT_STATE: {e}")
        return 3, report
    except StateError as e:
        report["failures"].append(f"STATE_ERROR: {e}")
        return 1, report
    except Exception as e:
        report["failures"].append(f"unexpected: {type(e).__name__}: {e}")
        return 1, report

    report["finished_at"] = _now_ist().isoformat()

    # Exit codes
    if report["failures"]:
        if any("source" in f.lower() or "adapter" in f.lower() for f in report["failures"]):
            return 2, report  # degraded
        return 1, report
    if mode == "FIXTURE":
        return 0, report
    return 0, report


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--policy", default=os.environ.get("POLICY_PATH", "config/policy.json"))
    parser.add_argument("--state", default=os.environ.get("STATE_PATH", "state.json"))
    parser.add_argument("--fixtures", default=os.environ.get("FIXTURES_PATH"))
    parser.add_argument("--mode", choices=["FIXTURE", "LIVE"], default=os.environ.get("ARTHA_MODE", "FIXTURE"))
    parser.add_argument("--dry-run", action="store_true", default=os.environ.get("DRY_RUN", "1") == "1")
    args = parser.parse_args()

    code, report = run(
        policy_path=args.policy,
        state_path=args.state,
        fixtures_path=args.fixtures,
        dry_run=args.dry_run,
        mode=args.mode,
    )
    print(json.dumps(report, indent=2, default=str))
    sys.exit(code)


if __name__ == "__main__":
    main()