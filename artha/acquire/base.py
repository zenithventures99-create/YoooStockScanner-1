# artha/acquire/base.py
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Dict, List

@dataclass
class AcquisitionResult:
    events: List[dict] = field(default_factory=list)
    source_status: Dict[str, Dict[str, Any]] = field(default_factory=dict)