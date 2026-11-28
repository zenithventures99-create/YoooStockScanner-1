"""Owner-decision registry and fail-closed policy enforcement for Project Artha."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, Tuple

MODULE_RS = "RESULTS_SURGE"
MODULE_BO = "BIG_ORDERS"

ENV_PRODUCTION = "PRODUCTION"
ENV_NON_PRODUCTION = "NON_PRODUCTION"
ENV_TEST_ONLY = "TEST_ONLY"

OD_IDS: Tuple[str, ...] = tuple(f"OD-{i}" for i in range(1, 10))

PERMITTED: Dict[str, Tuple[str, ...]] = {
    "OD-1": ("BOTH_TRIGGERS", "PROFIT_TRIGGER_ONLY"),
    "OD-2": ("INCLUSIVE_GE", "EXCLUSIVE_GT"),
    "OD-3": ("SIMPLIFIED_FOUR_TIERS", "SIMPLIFIED_WITH_DISPLAY_SUBLABELS"),
    "OD-4": ("CONSOLIDATED_PAT", "PAT_ATTRIBUTABLE_TO_OWNERS", "STANDALONE_PAT"),
    "OD-5": ("QUALIFY_WITH_FLAG", "FORCE_REVIEW"),
    "OD-6": ("REVENUE_FROM_OPERATIONS_QUARANTINE_FINANCIAL", "TOTAL_INCOME_QUARANTINE_FINANCIAL"),
    "OD-7": ("AUDITED_FY", "TTM"),
    "OD-8": ("ATTRIBUTABLE_SHARE_TAX_AS_DISCLOSED_INR_ONLY", "HEADLINE_TOTAL_TAX_AS_DISCLOSED_INR_ONLY"),
    "OD-9": ("FIRM_AWARD_ONLY", "FIRM_AWARD_AND_LOA"),
}

MODULE_DEPENDENCIES: Dict[str, Tuple[str, ...]] = {
    MODULE_RS: ("OD-1", "OD-2", "OD-4", "OD-5", "OD-6"),
    MODULE_BO: ("OD-7", "OD-8", "OD-9"),  # OD-3 is presentation-only
}

_PLACEHOLDERS = frozenset({
    "test_only", "test", "todo", "tbd", "none", "null", "x", "na", "n/a",
    "placeholder", "sample", "example", "dummy", "fake", "approved", "yes",
    "<<owner must supply>>",
})

MIN_REFERENCE_LENGTH = 8
_ALLOWED_TOP = frozenset({"environment", "decisions", "policy_version"})
_ALLOWED_ENTRY = frozenset({"status", "value", "approval_reference"})


class PolicyError(ValueError):
    pass


class PolicyNotConfiguredError(PolicyError):
    pass


def compute_fingerprint(decisions: Dict[str, Any]) -> str:
    parts = []
    for od in OD_IDS:
        entry = decisions.get(od) or {}
        status = entry.get("status") or "UNRESOLVED"
        value = entry.get("value")
        parts.append(f"{od}={status}:{value if value is not None else '-'}")
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]


def _no_duplicate_pairs(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise PolicyError(f"duplicate JSON key: {key!r}")
        out[key] = value
    return out


def parse_policy_document(text: str) -> Dict[str, Any]:
    try:
        doc = json.loads(text, object_pairs_hook=_no_duplicate_pairs)
    except json.JSONDecodeError as exc:
        raise PolicyError(f"policy is not valid JSON: {exc}") from exc
    if not isinstance(doc, dict):
        raise PolicyError("policy document must be an object")

    unknown = set(doc) - _ALLOWED_TOP
    if unknown:
        raise PolicyError(f"unknown top-level policy keys: {sorted(unknown)}")

    decisions = doc.get("decisions")
    if not isinstance(decisions, dict):
        raise PolicyError("policy must contain a 'decisions' object")

    unknown_ids = set(decisions) - set(OD_IDS)
    if unknown_ids:
        raise PolicyError(f"unknown decision ids: {sorted(unknown_ids)}")

    for od, entry in decisions.items():
        if not isinstance(entry, dict):
            raise PolicyError(f"{od}: entry must be an object")
        extra = set(entry) - _ALLOWED_ENTRY
        if extra:
            raise PolicyError(f"{od}: unknown entry keys {sorted(extra)}")

    environment = doc.get("environment", ENV_NON_PRODUCTION)
    if environment not in (ENV_PRODUCTION, ENV_NON_PRODUCTION, ENV_TEST_ONLY):
        raise PolicyError(f"invalid environment: {environment!r}")

    normalised: Dict[str, Any] = {"environment": environment, "decisions": {}}
    for od in OD_IDS:
        entry = decisions.get(od) or {}
        normalised["decisions"][od] = {
            "status": entry.get("status", "UNRESOLVED"),
            "value": entry.get("value"),
            "approval_reference": entry.get("approval_reference"),
        }

    normalised["policy_fingerprint"] = compute_fingerprint(normalised["decisions"])

    declared = doc.get("policy_version")
    if declared is not None:
        if not isinstance(declared, str) or declared != normalised["policy_fingerprint"]:
            raise PolicyError("declared policy_version does not match computed fingerprint")

    return normalised


def load_policy(path: str) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return parse_policy_document(fh.read())
    except (OSError, UnicodeDecodeError, PolicyError):
        empty = {
            "environment": ENV_NON_PRODUCTION,
            "decisions": {
                od: {"status": "UNRESOLVED", "value": None, "approval_reference": None}
                for od in OD_IDS
            },
        }
        empty["policy_fingerprint"] = compute_fingerprint(empty["decisions"])
        return empty


def decision_valid(entry: Any, od: str) -> bool:
    if od not in PERMITTED or not isinstance(entry, dict):
        return False
    if entry.get("status") != "OWNER_APPROVED":
        return False
    reference = entry.get("approval_reference")
    if not isinstance(reference, str):
        return False
    stripped = reference.strip()
    if len(stripped) < MIN_REFERENCE_LENGTH:
        return False
    if stripped.lower() in _PLACEHOLDERS or "<<" in stripped:
        return False
    value = entry.get("value")
    if not isinstance(value, str) or value not in PERMITTED[od]:
        return False
    return True


def unresolved_for_module(module: str, policy: Dict[str, Any]) -> Tuple[str, ...]:
    decisions = policy.get("decisions", {})
    return tuple(
        od for od in MODULE_DEPENDENCIES.get(module, ())
        if not decision_valid(decisions.get(od), od)
    )


def all_unresolved(policy: Dict[str, Any]) -> Tuple[str, ...]:
    decisions = policy.get("decisions", {})
    return tuple(od for od in OD_IDS if not decision_valid(decisions.get(od), od))


def read_od(policy: Dict[str, Any], od: str) -> str:
    entry = policy.get("decisions", {}).get(od)
    if not decision_valid(entry, od):
        raise PolicyError(f"{od} is not validly approved; caller must return POLICY_BLOCKED")
    return entry["value"]


def read_pat_floor_scope(policy: Dict[str, Any]) -> str:
    return read_od(policy, "OD-1")


def read_pat_floor_boundary(policy: Dict[str, Any]) -> str:
    return read_od(policy, "OD-2")


def read_tier_presentation(policy: Dict[str, Any]) -> str:
    entry = policy.get("decisions", {}).get("OD-3")
    if not decision_valid(entry, "OD-3"):
        return "SIMPLIFIED_FOUR_TIERS"
    return entry["value"]


def read_pat_definition(policy: Dict[str, Any]) -> str:
    return read_od(policy, "OD-4")


def read_exceptional_policy(policy: Dict[str, Any]) -> str:
    return read_od(policy, "OD-5")


def read_revenue_metric(policy: Dict[str, Any]) -> str:
    return read_od(policy, "OD-6")


def read_order_denominator(policy: Dict[str, Any]) -> str:
    return read_od(policy, "OD-7")


def read_order_value_basis(policy: Dict[str, Any]) -> str:
    return read_od(policy, "OD-8")


def read_award_categories(policy: Dict[str, Any]) -> str:
    return read_od(policy, "OD-9")


def production_eligible(policy: Dict[str, Any]) -> bool:
    if policy.get("environment") != ENV_PRODUCTION:
        return False
    return not all_unresolved(policy)


__all__ = [
    "MODULE_RS", "MODULE_BO",
    "ENV_PRODUCTION", "ENV_NON_PRODUCTION", "ENV_TEST_ONLY",
    "OD_IDS", "PERMITTED", "MODULE_DEPENDENCIES",
    "PolicyError", "PolicyNotConfiguredError",
    "compute_fingerprint", "parse_policy_document", "load_policy",
    "decision_valid", "unresolved_for_module", "all_unresolved",
    "read_od", "read_pat_floor_scope", "read_pat_floor_boundary",
    "read_tier_presentation", "read_pat_definition", "read_exceptional_policy",
    "read_revenue_metric", "read_order_denominator", "read_order_value_basis",
    "read_award_categories", "production_eligible",
]