"""
Base validation result types and shared helpers.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Severity(str, Enum):
    FLAG = "FLAG"           # Needs human review.
    AUTO_FIX = "AUTO_FIX"  # Deterministic fix available (only applied if CORRECTION_MODE=auto).
    INFO = "INFO"           # Soft warning.


@dataclass
class Issue:
    code: str                   # e.g. "MISSING_ATTACHMENT".
    severity: Severity
    message: str
    field: str | None = None    # Which field is affected.
    suggested_value: Any = None # For auto-fix or AI suggestion.


@dataclass
class ValidationResult:
    record_type: str            # "BankTransaction" | "Bill".
    record_id: str
    record_ref: str             # Human-readable ref (e.g. "INV-001", "BA-00123").
    issues: list[Issue] = field(default_factory=list)
    ai_suggestions: dict = field(default_factory=dict)  # Populated by AI handler.
    approved: bool | None = None  # None = pending.

    @property
    def has_issues(self) -> bool:
        return bool(self.issues)

    @property
    def needs_review(self) -> bool:
        return any(i.severity in (Severity.FLAG,) for i in self.issues)
