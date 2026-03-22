"""
Deterministic validators for Bills (ACCPAY invoices — includes email-parsed receipts).
"""

from __future__ import annotations
import yaml
from pathlib import Path
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


def validate_bills(
    bills: list[dict],
    ref: ReferenceData,
) -> list[ValidationResult]:
    rules = _load_rules()
    results: list[ValidationResult] = []

    for bill in bills:
        issues: list[Issue] = []
        bill_id = bill.get("InvoiceID", "")
        bill_ref = bill.get("InvoiceNumber") or bill.get("Reference") or bill_id[:8]

        # 1. Missing attachment (receipts from email should have one).
        if not bill.get("HasAttachments", False) and not is_skipped(bill_id, "MISSING_ATTACHMENT"):
            issues.append(Issue(
                code="MISSING_ATTACHMENT",
                severity=Severity.FLAG,
                message="No attachment — expected a receipt.",
                field="HasAttachments",
            ))

        # 2. No contact/supplier.
        _PLACEHOLDER_NAMES = {"no contact", "unknown", ""}
        contact = bill.get("Contact", {})
        contact_name = (contact.get("Name") or "").strip().lower()
        if not contact.get("ContactID") or contact_name in _PLACEHOLDER_NAMES:
            issues.append(Issue(
                code="MISSING_SUPPLIER",
                severity=Severity.FLAG,
                message="Bill has no supplier contact assigned.",
                field="Contact",
            ))

        # 3. Line item validation.
        for line in bill.get("LineItems", []):
            code = line.get("AccountCode", "")

            if not code:
                issues.append(Issue(
                    code="MISSING_ACCOUNT_CODE",
                    severity=Severity.FLAG,
                    message="Line item has no account code.",
                    field="AccountCode",
                ))
            elif code not in ref.accounts:
                issues.append(Issue(
                    code="INVALID_ACCOUNT_CODE",
                    severity=Severity.FLAG,
                    message=f"Account code '{code}' not in Chart of Accounts.",
                    field="AccountCode",
                ))
            else:
                tax_name = line.get("TaxType", "")
                if tax_name in rules.get("invalid_expense_tax_rates", []):
                    issues.append(Issue(
                        code="INVALID_TAX_RATE",
                        severity=Severity.FLAG,
                        message=f"Tax rate '{tax_name}' is an output tax — wrong for a bill.",
                        field="TaxType",
                    ))

            if not line.get("Description", "").strip():
                issues.append(Issue(
                    code="MISSING_DESCRIPTION",
                    severity=Severity.FLAG,
                    message="Line item has no description.",
                    field="Description",
                ))

            if not line.get("TaxType", "").strip():
                issues.append(Issue(
                    code="MISSING_TAX_RATE",
                    severity=Severity.FLAG,
                    message="Line item has no tax rate.",
                    field="TaxType",
                ))

        # 4. Tracking categories.
        required_cats = {c.lower() for c in rules.get("required_tracking_categories", [])}
        if required_cats:
            assigned_cats = set()
            for line in bill.get("LineItems", []):
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

        # 5. Zero or missing amount — flag bills where total is 0 OR any line item.
        # has a null/missing UnitAmount (distinct from an intentional £0 line).
        try:
            total = float(bill.get("Total") or 0)
        except (TypeError, ValueError):
            total = 0.0
        line_amounts_missing = any(
            line.get("UnitAmount") is None
            for line in bill.get("LineItems", [])
        )
        if total == 0.0 or line_amounts_missing:
            issues.append(Issue(
                code="ZERO_AMOUNT",
                severity=Severity.FLAG,
                message=(
                    "Bill total is £0.00 — amount likely missing or incorrectly entered."
                    if total == 0.0
                    else "One or more line items have no amount set."
                ),
                field="LineItems",
            ))

        # 6. Foreign currency — flag if there's an attachment (strategy checks actual currency).
        if bill.get("HasAttachments", False):
            issues.append(Issue(
                code="FOREIGN_CURRENCY_AMOUNT",
                severity=Severity.INFO,
                message="Bill has attachment — checking for foreign currency amounts.",
                field="LineItems",
            ))

        # 6. Draft bill with no due date (common with email-parsed receipts).
        if bill.get("Status") == "DRAFT" and not bill.get("DueDate"):
            issues.append(Issue(
                code="MISSING_DUE_DATE",
                severity=Severity.INFO,
                message="Draft bill has no due date.",
                field="DueDate",
            ))

        results.append(ValidationResult(
            record_type="Bill",
            record_id=bill_id,
            record_ref=bill_ref,
            issues=issues,
        ))

    return [r for r in results if r.has_issues]
