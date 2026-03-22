"""
Shared constants for the Xero expense audit tool.

DISCLAIMER: This tool is provided "as is", without warranty of any kind. Use at
your own risk. The author(s) accept no liability for any damages, data loss, or
incorrect financial transactions resulting from the use of this software. Always
verify changes in Xero before approving. Not a substitute for professional
accounting advice.
"""

# Xero invoice/bill statuses.
DRAFT = "DRAFT"
SUBMITTED = "SUBMITTED"
AUTHORISED = "AUTHORISED"
PAID = "PAID"
VOIDED = "VOIDED"
DELETED = "DELETED"

# Statuses indicating a bill is no longer actionable.
TERMINAL_STATUSES = frozenset({AUTHORISED, PAID, VOIDED, DELETED})
