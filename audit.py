#!/usr/bin/env python3
"""
xero-expense-audit — validate Xero bills, flag issues, and queue corrections.

DISCLAIMER: This tool is provided "as is", without warranty of any kind. Use at
your own risk. The author(s) accept no liability for any damages, data loss, or
incorrect financial transactions resulting from the use of this software. Always
verify changes in Xero before approving. Not a substitute for professional
accounting advice.

Usage:
  python audit.py run                            # fetch + validate + queue flagged items.
  python audit.py review                         # interactively review queued items.
  python audit.py status                         # show queue summary.
  python audit.py clear                          # remove approved items from queue.
  python audit.py fix-bill [INVOICE_ID]          # fix a single bill interactively.
  python audit.py fix-next-bill                  # loop through fixable bills.
  python audit.py mark-as-director-loan-payment  # mark authorised bills as paid via director loan.
  python audit.py director-loan-balance          # show director loan balance summary.
  python audit.py setup-auth                     # one-time OAuth 2.0 browser login.
"""

import json
from pathlib import Path

import click
from dotenv import load_dotenv

load_dotenv()

_audit_config: dict | None = None


def load_audit_config() -> dict:
    """Load and cache config/xero.config.json."""
    global _audit_config
    if _audit_config is None:
        cfg_path = Path(__file__).parent / "config" / "xero.config.json"
        with open(cfg_path) as f:
            _audit_config = json.load(f)
    return _audit_config


@click.group()
def cli():
    """Xero Expense Auditor — validate, flag, and queue corrections."""
    pass


# Register commands from sub-modules.
from src.commands import run, review, queue_mgmt, fix_bill, director_loan, setup  # noqa: E402.

run.register(cli, load_audit_config)
review.register(cli, load_audit_config)
queue_mgmt.register(cli, load_audit_config)
fix_bill.register(cli, load_audit_config)
director_loan.register(cli, load_audit_config)
setup.register(cli, load_audit_config)


if __name__ == "__main__":
    cli()
