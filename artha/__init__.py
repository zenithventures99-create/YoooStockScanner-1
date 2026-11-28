"""Project Artha — evidence-driven Indian listed-company screening.
Authorised modules: RESULTS_SURGE and BIG_ORDERS only.
Runtime core uses the standard library only.
"""

from __future__ import annotations

__version__ = "2.1.0"
version = __version__
AUTHOR = "Project Artha Implementation Team"

AUTHORIZED_MODULES = frozenset({"RESULTS_SURGE", "BIG_ORDERS"})
MAX_RECURRING_COST = 0  # ₹0 per month

__all__ = [
    "__version__",
    "version",
    "AUTHOR",
    "AUTHORIZED_MODULES",
    "MAX_RECURRING_COST",
]