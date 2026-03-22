"""
Reference data loader — Chart of Accounts, tax rates, tracking categories, suppliers.
Cached in memory per run; refreshed at start of each audit.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from .xero_client import get


@dataclass
class ReferenceData:
    accounts: dict[str, dict] = field(default_factory=dict)          # code -> account.
    tax_rates: dict[str, dict] = field(default_factory=dict)         # name -> tax rate.
    tracking_categories: list[dict] = field(default_factory=list)
    suppliers: dict[str, dict] = field(default_factory=dict)         # contactID -> contact.


def load_reference_data() -> ReferenceData:
    ref = ReferenceData()

    # Chart of Accounts (Code is optional in Xero — skip accounts without one).
    data = get("Accounts", params={"where": 'Status=="ACTIVE"'})
    for acct in data.get("Accounts", []):
        if acct.get("Code"):
            ref.accounts[acct["Code"]] = acct

    # Tax Rates.
    data = get("TaxRates", params={"where": 'Status=="ACTIVE"'})
    for rate in data.get("TaxRates", []):
        ref.tax_rates[rate["Name"]] = rate

    # Tracking Categories.
    data = get("TrackingCategories", params={"where": 'Status=="ACTIVE"'})
    ref.tracking_categories = data.get("TrackingCategories", [])

    # Suppliers.
    data = get("Contacts", params={"where": 'IsSupplier==true AND ContactStatus=="ACTIVE"'})
    for contact in data.get("Contacts", []):
        ref.suppliers[contact["ContactID"]] = contact

    return ref
