"""
Approval queue — stores flagged records for human review.
Backed by a local (git-ignored) JSON file (simple, no DB needed).
"""

from __future__ import annotations
import json
import os
from datetime import datetime, UTC
from pathlib import Path
from .validators.base import ValidationResult, Issue

QUEUE_PATH = Path(__file__).parent.parent / "queue.json"


def _load() -> list[dict]:
    if QUEUE_PATH.exists():
        with open(QUEUE_PATH) as f:
            return json.load(f)
    return []


def _save(items: list[dict]) -> None:
    with open(QUEUE_PATH, "w") as f:
        json.dump(items, f, indent=2, default=str)


def enqueue(result: ValidationResult, ai_suggestions: dict | None = None) -> None:
    items = _load()
    entry = {
        "id": result.record_id,
        "type": result.record_type,
        "ref": result.record_ref,
        "queued_at": datetime.now(UTC).isoformat(),
        "approved": None,
        "issues": [
            {
                "code": i.code,
                "severity": i.severity,
                "message": i.message,
                "field": i.field,
                "suggested_value": i.suggested_value,
            }
            for i in result.issues
        ],
        "ai_suggestions": ai_suggestions or {},
    }
    # Avoid duplicates.
    if not any(i["id"] == result.record_id for i in items):
        items.append(entry)
    _save(items)


def list_pending() -> list[dict]:
    return [i for i in _load() if i["approved"] is None]


def approve(record_id: str) -> None:
    items = _load()
    for item in items:
        if item["id"] == record_id:
            item["approved"] = True
    _save(items)


def reject(record_id: str) -> None:
    items = _load()
    for item in items:
        if item["id"] == record_id:
            item["approved"] = False
    _save(items)


def clear_approved() -> int:
    items = _load()
    before = len(items)
    items = [i for i in items if i["approved"] is not True]
    _save(items)
    return before - len(items)
