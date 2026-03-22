"""Run command — fetch expenses from Xero, validate, and queue flagged items."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, UTC

import click
from rich.console import Console
from rich.table import Table

from ..constants import DRAFT

console = Console()


def _xero_date_str(xero_date: str) -> str:
    """Convert Xero /Date(timestamp)/ to readable YYYY-MM-DD string."""
    try:
        ts = int(xero_date.strip("/Date()").split("+")[0].split("-")[0])
        return datetime.utcfromtimestamp(ts / 1000).strftime("%Y-%m-%d")
    except Exception:
        return "—"


def register(cli: click.Group, load_audit_config) -> None:
    """Register the run command on the CLI group."""

    @cli.command()
    @click.option(
        "--days",
        default=int(os.environ.get("AUDIT_LOOKBACK_DAYS", 90)),
        show_default=True,
        help="Lookback window (days) to audit.",
    )
    @click.option("--no-ai", is_flag=True, help="Skip AI suggestions (deterministic only).")
    @click.option("--auto-correct", is_flag=True, help="Auto-correct fixable issues via strategy chains.")
    @click.option("--fix-suppliers", is_flag=True, help="Auto-fix missing supplier defaults.")
    def run(days: int, no_ai: bool, auto_correct: bool, fix_suppliers: bool):
        """Fetch expenses from Xero and run validation."""
        from ..reference_data_loader import load_reference_data
        from ..xero_client import paginate
        from ..ai_handler import get_ai_suggestions
        from ..queue import enqueue

        console.rule("[bold blue]Xero Expense Audit")
        console.print(f"  Auditing last [bold]{days}[/bold] days...")

        console.print("  Loading reference data (CoA, tax rates, contacts)...")
        ref = load_reference_data()
        console.print(f"  ✓ {len(ref.accounts)} accounts, {len(ref.tax_rates)} tax rates")

        since = (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%d")

        # Fetch bills in DRAFT only — SUBMITTED = awaiting approval, already past fix stage.
        xero_date = since.replace("-", ",")
        console.print(f"  Fetching draft bills since {since}...")
        bills = list(paginate(
            "Invoices",
            "Invoices",
            params={
                "where": f'Type=="ACCPAY" AND Date>=DateTime({xero_date}) AND Status=="{DRAFT}"',
                "unitdp": 4,
            },
        ))
        console.print(f"  ✓ {len(bills)} draft bills")

        console.print("  Running deterministic validation...")
        from ..validators.bills import validate_bills
        all_results = validate_bills(bills, ref)
        console.print(f"  ✓ {len(all_results)} records flagged")

        if not all_results:
            console.print("\n[green]✅ All clear — no issues found.[/green]")
            return

        if not no_ai and all_results:
            console.print(f"  Running AI triage on {len(all_results)} flagged records...")
            coa_list = list(ref.accounts.values())
            records_by_id = {b["InvoiceID"]: b for b in bills}

            for result in all_results:
                record = records_by_id.get(result.record_id, {})
                ai_out = get_ai_suggestions(result, record, coa_list)
                result.ai_suggestions = ai_out
            console.print("  ✓ AI triage complete")

        audit_cfg = load_audit_config()
        if auto_correct:
            from ..fix_bills import apply_fixes, push_fix
            records_by_id_fix = {b["InvoiceID"]: b for b in bills}
            corrected = 0
            for result in all_results:
                bill = records_by_id_fix.get(result.record_id, {})
                patch = apply_fixes(bill, result.issues, ref, audit_cfg)
                if patch:
                    ok = push_fix(result.record_id, patch, bill)
                    if ok:
                        corrected += 1
                        console.print(f"  ✓ Auto-corrected {result.record_ref}: {', '.join(patch.keys())}")
                    else:
                        console.print(f"  ✗ Failed to push fix for {result.record_ref}")
            console.print(f"  ✓ {corrected} bill(s) auto-corrected")

        for result in all_results:
            enqueue(result, result.ai_suggestions)
        console.print(f"  ✓ {len(all_results)} records added to queue")

        if audit_cfg.get("supplier_audit", {}).get("enabled"):
            from ..supplier_audit import run_supplier_audit, push_supplier_fix
            supplier_issues = run_supplier_audit(ref, bills, audit_cfg)
            if supplier_issues:
                s_table = Table(title="Supplier Issues", show_lines=True)
                s_table.add_column("Supplier", style="cyan")
                s_table.add_column("Field", style="yellow")
                s_table.add_column("Inferred Value", style="green")
                s_table.add_column("Source Bill", style="white")
                for si in supplier_issues:
                    s_table.add_row(si["contact_name"], si["field"], si["inferred_value"], si["source_bill_ref"])
                console.print(s_table)

                if fix_suppliers:
                    for si in supplier_issues:
                        ok = push_supplier_fix(si["contact_id"], si["field"], si["inferred_value"])
                        status_str = "[green]✓[/green]" if ok else "[red]✗[/red]"
                        console.print(f"  {status_str} {si['contact_name']}.{si['field']} = {si['inferred_value']}")
            else:
                console.print("  ✓ No supplier defaults missing")

        meta_by_id = {}
        for b in bills:
            bid = b.get("InvoiceID", "")
            contact = b.get("Contact", {}).get("Name", "—")
            total = b.get("Total", "")
            currency = b.get("CurrencyCode", "")
            date = _xero_date_str(b.get("Date", ""))
            url = f"https://go.xero.com/AccountsPayable/Edit.aspx?InvoiceID={bid}"
            meta_by_id[bid] = {"contact": contact, "total": f"{currency} {total}", "date": date, "url": url}

        table = Table(title="Issues Found", show_lines=True)
        table.add_column("#", style="dim", width=3)
        table.add_column("Type", style="cyan")
        table.add_column("Supplier", style="white")
        table.add_column("Amount", style="white", justify="right")
        table.add_column("Date", style="white")
        table.add_column("Issues", style="yellow")
        for idx, r in enumerate(all_results, 1):
            codes = ", ".join(i.code for i in r.issues)
            m = meta_by_id.get(r.record_id, {})
            table.add_row(
                str(idx),
                r.record_type,
                m.get("contact", "—"),
                m.get("total", "—"),
                m.get("date", "—"),
                codes,
            )
        console.print(table)

        console.print("\n[bold]Xero Links:[/bold]")
        for idx, r in enumerate(all_results, 1):
            m = meta_by_id.get(r.record_id, {})
            console.print(f"  [{idx}] {m.get('contact', r.record_ref)}: {m.get('url', '—')}")

        console.print(f"\n[yellow]⚠ {len(all_results)} items queued for review.[/yellow] Run [bold]python audit.py review[/bold] to action them.")
