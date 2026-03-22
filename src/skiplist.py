"""
Audit state — persistent state for the expense audit tool.
Stored in (git-ignored) audit-state.json.

Current sections:
  exceptions  — records manually excluded from specific checks.
"""

from __future__ import annotations
import json
from pathlib import Path

STATE_PATH = Path(__file__).parent.parent / "audit-state.json"


def _load() -> dict:
    if STATE_PATH.exists():
        with open(STATE_PATH) as f:
            return json.load(f)
    return {"exceptions": {}}


def _save(data: dict) -> None:
    with open(STATE_PATH, "w") as f:
        json.dump(data, f, indent=2)


def is_skipped(record_id: str, check: str) -> bool:
    """Return True if a specific check is excepted for this record."""
    data = _load()
    entry = data.get("exceptions", {}).get(record_id, {})
    return check in entry.get("skip_checks", []) or entry.get("skip_all", False)


def skip_record(record_id: str, record_ref: str, checks: list[str], reason: str = "") -> None:
    """Add exception for specific checks on a record."""
    data = _load()
    exceptions = data.setdefault("exceptions", {})
    entry = exceptions.get(record_id, {"ref": record_ref, "skip_checks": []})
    for check in checks:
        if check not in entry["skip_checks"]:
            entry["skip_checks"].append(check)
    if reason:
        entry["reason"] = reason
    exceptions[record_id] = entry
    _save(data)


def list_exceptions() -> dict:
    return _load().get("exceptions", {})
