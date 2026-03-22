"""
Supplier contact audit — infer missing PurchasesDefaultAccountCode and DefaultTaxType
from most recent bills.
"""

from __future__ import annotations

import logging

from .xero_client import put
from .reference_data_loader import ReferenceData

logger = logging.getLogger(__name__)


def run_supplier_audit(
    ref: ReferenceData,
    bills: list[dict],
    config: dict,
) -> list[dict]:
    """Check each supplier contact for missing defaults, infer from recent bills."""
    sa_config = config.get("supplier_audit", {})
    if not sa_config.get("enabled"):
        return []

    # Build supplier -> bills index (most recent first).
    bills_by_supplier: dict[str, list[dict]] = {}
    for b in bills:
        cid = b.get("Contact", {}).get("ContactID")
        if cid:
            bills_by_supplier.setdefault(cid, []).append(b)

    # Sort each supplier's bills by date descending.
    for cid in bills_by_supplier:
        bills_by_supplier[cid].sort(key=lambda b: b.get("Date", ""), reverse=True)

    results: list[dict] = []

    for contact_id, contact in ref.suppliers.items():
        contact_name = contact.get("Name", contact_id[:8])

        fields_to_check: list[tuple[str, str, bool]] = []
        if sa_config.get("infer_missing_purchases_account"):
            fields_to_check.append(("PurchasesDefaultAccountCode", "AccountCode", not contact.get("PurchasesDefaultAccountCode")))
        if sa_config.get("infer_missing_tax_rate"):
            fields_to_check.append(("DefaultTaxType", "TaxType", not contact.get("DefaultTaxType")))

        for field_name, line_field, is_missing in fields_to_check:
            if not is_missing:
                continue

            supplier_bills = bills_by_supplier.get(contact_id, [])
            if not supplier_bills:
                continue

            # Take from first line item of most recent bill.
            most_recent = supplier_bills[0]
            bill_ref = most_recent.get("InvoiceNumber") or most_recent.get("Reference") or most_recent.get("InvoiceID", "")[:8]
            lines = most_recent.get("LineItems", [])
            if not lines:
                continue

            inferred = lines[0].get(line_field, "")
            if inferred:
                results.append({
                    "contact_id": contact_id,
                    "contact_name": contact_name,
                    "field": field_name,
                    "inferred_value": inferred,
                    "source_bill_ref": bill_ref,
                })

    return results


def push_supplier_fix(contact_id: str, field: str, value: str) -> bool:
    """PUT a single field update to a Xero contact. Returns True on success."""
    try:
        put(f"Contacts/{contact_id}", {field: value})
        return True
    except Exception:
        logger.exception("push_supplier_fix failed for contact %s", contact_id)
        return False
