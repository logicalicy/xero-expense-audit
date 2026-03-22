"""
Claude AI handler — handles ambiguous cases that deterministic rules can't resolve.
Structured JSON output only. No prose.

Model and thresholds are configured in config/ai.yaml.
"""

from __future__ import annotations
import json
import os
from pathlib import Path
import yaml
import anthropic
from .validators.base import ValidationResult

_config_path = Path(__file__).parent.parent / "config" / "ai.yaml"
with open(_config_path) as _f:
    _ai_cfg = yaml.safe_load(_f)

TEXT_MODEL: str = _ai_cfg.get("text_model", "claude-haiku-4-5")
VISION_MODEL: str = _ai_cfg.get("vision_model", "claude-haiku-4-5")
MAX_TOKENS: int = _ai_cfg.get("max_tokens", 512)
MIN_AUTO_CORRECT_CONFIDENCE: float = _ai_cfg.get("min_auto_correct_confidence", 0.7)

client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

def _build_system_prompt() -> str:
    return f"""You are an expense auditor assistant. You receive a Xero expense record and a list of issues found by deterministic rules.

Your job: suggest corrections for ambiguous issues — wrong account codes, unclear descriptions, mismatched suppliers.

RULES:
- Reply ONLY with a JSON object. No prose, no explanation.
- Structure: {{ "suggestions": [ {{ "field": "AccountCode", "suggested_value": "420", "confidence": 0.85, "reason": "..." }} ] }}
- Confidence: 0.0–1.0. Below {MIN_AUTO_CORRECT_CONFIDENCE} = low confidence, do not suggest.
- Never suggest values you are not confident about.
- If no useful suggestions: return {{ "suggestions": [] }}
"""

SYSTEM_PROMPT = _build_system_prompt()


def get_ai_suggestions(result: ValidationResult, record: dict, chart_of_accounts: list[dict]) -> dict:
    """
    Ask Claude Haiku for suggestions on a flagged record.
    Returns a dict of field -> suggestion.
    """
    coa_summary = [
        {"code": a["Code"], "name": a["Name"], "type": a["Type"]}
        for a in chart_of_accounts[:100]  # keep context small.
    ]

    user_message = json.dumps({
        "record_type": result.record_type,
        "record": {
            "ref": result.record_ref,
            "contact": record.get("Contact", {}).get("Name", ""),
            "total": record.get("Total"),
            "currency": record.get("CurrencyCode", ""),
            "line_items": [
                {
                    "description": l.get("Description", ""),
                    "account_code": l.get("AccountCode", ""),
                    "tax_type": l.get("TaxType", ""),
                    "quantity": l.get("Quantity"),
                    "unit_amount": l.get("UnitAmount"),
                }
                for l in record.get("LineItems", [])
            ],
        },
        "issues": [
            {"code": i.code, "field": i.field, "message": i.message}
            for i in result.issues
        ],
        "available_account_codes": coa_summary,
    }, indent=2)

    message = client.messages.create(
        model=TEXT_MODEL,
        max_tokens=MAX_TOKENS,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    )

    try:
        raw = message.content[0].text.strip()
        return json.loads(raw)
    except (json.JSONDecodeError, IndexError):
        return {"suggestions": []}
