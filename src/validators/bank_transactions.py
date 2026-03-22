"""
Deterministic validators for BankTransactions (bank feed items).
"""

from __future__ import annotations
import yaml
from pathlib import Path
from datetime import datetime, timedelta
from collections import defaultdict
from ..reference_data_loader import ReferenceData
from ..skiplist import is_skipped
from .base import Issue, Severity, ValidationResult

_rules: dict | None = None


def _load_rules() -> dict:
    global _rules
    if _rules is None:
        rules_path = Path(__file__).parent.parent.parent / "config" / "rules.yaml"
        with open(rules_path) as f:
            _rules = yaml.safe_load(f)
    return _rules


def validate_bank_transactions(
    transactions: list[dict],
    ref: ReferenceData,
) -> list[ValidationResult]:
    rules = _load_rules()
    results: list[ValidationResult] = []

    # Build lookup for duplicate detection.
    by_supplier_amount: dict[tuple, list[dict]] = defaultdict(list)
    for tx in transactions:
        key = (
            tx.get("Contact", {}).get("ContactID", ""),
            tx.get("Total", 0),
            tx.get("CurrencyCode", ""),
        )
        by_supplier_amount[key].append(tx)

    for tx in transactions:
        issues: list[Issue] = []
        tx_id = tx.get("BankTransactionID", "")
        tx_ref = tx.get("Reference") or tx_id[:8]

        # 1. Missing attachment.
        if not tx.get("HasAttachments", False) and not is_skipped(tx_id, "MISSING_ATTACHMENT"):
            issues.append(Issue(
                code="MISSING_ATTACHMENT",
                severity=Severity.FLAG,
                message="No receipt or attachment found.",
                field="HasAttachments",
            ))

        # 2. Line item account code validation.
        for line in tx.get("LineItems", []):
            code = line.get("AccountCode", "")
            if code and code not in ref.accounts:
                issues.append(Issue(
                    code="INVALID_ACCOUNT_CODE",
                    severity=Severity.FLAG,
                    message=f"Account code '{code}' not found in Chart of Accounts.",
                    field="AccountCode",
                    suggested_value=None,
                ))
            elif code:
                acct = ref.accounts[code]
                acct_type = acct.get("Type", "")
                # Check tax rate makes sense for account type.
                tax_name = line.get("TaxType", "")
                if tax_name in rules.get("invalid_expense_tax_rates", []):
                    issues.append(Issue(
                        code="INVALID_TAX_RATE",
                        severity=Severity.FLAG,
                        message=f"Tax rate '{tax_name}' is an output/sales tax — unexpected on expense account '{code}'.",
                        field="TaxType",
                    ))

        # 3. Tracking categories.
        required_cats = {c.lower() for c in rules.get("required_tracking_categories", [])}
        if required_cats:
            assigned_cats = set()
            for line in tx.get("LineItems", []):
                for tc in line.get("Tracking", []):
                    assigned_cats.add(tc.get("Name", "").lower())
            missing = required_cats - assigned_cats
            if missing:
                issues.append(Issue(
                    code="MISSING_TRACKING_CATEGORY",
                    severity=Severity.FLAG,
                    message=f"Missing tracking category: {', '.join(missing)}.",
                    field="Tracking",
                ))

        # 4. Duplicate detection.
        dup_window = rules.get("duplicate_window_days", 7)
        dup_ignore = rules.get("duplicate_ignore_below", 5.0)
        total = float(tx.get("Total", 0))
        if total >= dup_ignore:
            key = (
                tx.get("Contact", {}).get("ContactID", ""),
                tx.get("Total", 0),
                tx.get("CurrencyCode", ""),
            )
            candidates = by_supplier_amount.get(key, [])
            tx_date = _parse_date(tx.get("Date", ""))
            dups = [
                c for c in candidates
                if c["BankTransactionID"] != tx_id
                and tx_date
                and abs((_parse_date(c.get("Date", "")) - tx_date).days) <= dup_window
            ]
            if dups:
                issues.append(Issue(
                    code="POSSIBLE_DUPLICATE",
                    severity=Severity.FLAG,
                    message=f"Possible duplicate: {len(dups)} similar transaction(s) within {dup_window} days.",
                    field="Total",
                ))

        results.append(ValidationResult(
            record_type="BankTransaction",
            record_id=tx_id,
            record_ref=tx_ref,
            issues=issues,
        ))

    return [r for r in results if r.has_issues]


def _parse_date(xero_date: str) -> datetime:
    """Parse Xero's /Date(timestamp)/ format."""
    try:
        ts = int(xero_date.strip("/Date()").split("+")[0].split("-")[0])
        return datetime.utcfromtimestamp(ts / 1000)
    except Exception:
        return datetime.utcnow()
