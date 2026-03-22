"""Review command — interactively review queued corrections."""

from __future__ import annotations

import click
from rich.console import Console
from rich import print as rprint

from ..constants import AUTHORISED, TERMINAL_STATUSES

console = Console()


def register(cli: click.Group, load_audit_config) -> None:
    """Register the review command on the CLI group."""

    @cli.command()
    @click.option("--dry-run", is_flag=True, help="Show what would happen without writing to Xero.")
    def review(dry_run: bool):
        """Review flagged bills: apply fixes and authorise for payment, or reject.

        Approve = auto-correct any fixable issues + set Status: AUTHORISED in Xero.
        Reject  = leave the bill untouched in Xero, mark as rejected in the queue.
        Skip    = defer to next review session.
        """
        from ..queue import list_pending, approve, reject
        from ..fix_bills import apply_fixes, push_fix
        from ..reference_data_loader import load_reference_data
        from ..xero_client import get as xero_get

        pending = list_pending()
        if not pending:
            console.print("[green]✅ Nothing in the queue.[/green]")
            return

        audit_cfg = load_audit_config()
        auto_correct_cfg = audit_cfg.get("auto_correct", {})

        console.print(f"[bold]{len(pending)} bill(s) pending review.[/bold]")
        if dry_run:
            console.print("[dim]Dry run — nothing will be written to Xero.[/dim]")
        console.print()

        ref = None
        approved_count = rejected_count = skipped_count = 0

        for item in pending:
            try:
                bill_data = xero_get(f"Invoices/{item['id']}", params={"unitdp": 4})
                bill = bill_data.get("Invoices", [{}])[0] if bill_data.get("Invoices") else {}
            except Exception:
                console.print(f"[yellow]  ✗ Could not fetch bill {item['id']} from Xero — removing from queue (likely deleted/voided).[/yellow]")
                reject(item["id"])
                skipped_count += 1
                continue

            bill_status = bill.get("Status", "")
            if bill_status in TERMINAL_STATUSES:
                console.print(f"  [dim]Bill {item['id'][:8]} is {bill_status} — removing from queue.[/dim]")
                approve(item["id"])
                skipped_count += 1
                continue

            supplier = bill.get("Contact", {}).get("Name", "—")
            total = bill.get("Total", "")
            currency = bill.get("CurrencyCode", "")
            status = bill.get("Status", "")
            xero_url = f"https://go.xero.com/AccountsPayable/Edit.aspx?InvoiceID={item['id']}"

            console.rule(f"[cyan]{item['ref']}[/cyan]")
            console.print(f"  Supplier: [cyan]{supplier}[/cyan]  Amount: {currency} {total}  Xero status: {status}")
            console.print(f"  [dim]{xero_url}[/dim]")

            if ref is None:
                ref = load_reference_data()
            from ..validators.bills import validate_bills as _validate
            live_results = _validate([bill], ref)
            if not live_results:
                console.print("  [green]✅ No issues remaining — removing from queue.[/green]")
                approve(item["id"])
                skipped_count += 1
                continue
            issues = live_results[0].issues

            console.print(f"\n  Issues ({len(issues)}):")
            for issue in issues:
                rule = auto_correct_cfg.get(issue.code)
                fix_label = "[dim](auto-fix available)[/dim]" if rule and rule.get("enabled") else "[yellow](manual fix needed)[/yellow]"
                rprint(f"    [yellow]•[/yellow] {issue.code}: {issue.message} {fix_label}")

            has_fixable = any(auto_correct_cfg.get(i.code, {}).get("enabled") for i in issues)
            patch = {}
            if has_fixable:
                patch = apply_fixes(bill, issues, ref, audit_cfg)
                if patch:
                    console.print("\n  [bold]Fixes that will be applied on approve:[/bold]")
                    for field, value in patch.items():
                        if field == "LineItems":
                            for idx, line in enumerate(value):
                                for k, v in line.items():
                                    orig = bill.get("LineItems", [{}])[idx].get(k, "") if idx < len(bill.get("LineItems", [])) else ""
                                    if v != orig:
                                        console.print(f"    [green]LineItems[{idx}].{k}:[/green] {orig!r} → {v!r}")
                        else:
                            orig = bill.get(field, "")
                            console.print(f"    [green]{field}:[/green] {orig!r} → {value!r}")

            issues_remaining = [i for i in issues if not (auto_correct_cfg.get(i.code, {}).get("enabled"))]
            if issues_remaining:
                console.print(f"\n  [yellow]⚠ {len(issues_remaining)} issue(s) cannot be auto-fixed — bill will be authorised with them outstanding.[/yellow]")

            contact_id = bill.get("Contact", {}).get("ContactID")
            if contact_id:
                try:
                    from ..xero_client import get as _xero_get
                    contact_data = _xero_get(f"Contacts/{contact_id}")
                    contact = contact_data.get("Contacts", [{}])[0]
                    default_account = contact.get("PurchasesDefaultAccountCode", "")
                    default_tax = contact.get("PurchasesDefaultTaxType", "")

                    for idx, line in enumerate(bill.get("LineItems", [])):
                        patch_line = (patch.get("LineItems") or [{}] * (idx + 1))[idx] if idx < len(patch.get("LineItems") or []) else {}
                        effective_account = patch_line.get("AccountCode") or line.get("AccountCode", "")
                        effective_tax = patch_line.get("TaxType") or line.get("TaxType", "")

                        if default_account and effective_account and effective_account != default_account:
                            console.print(
                                f"\n  [yellow]⚠ Account code mismatch (line {idx}):[/yellow] "
                                f"bill has [bold]{effective_account}[/bold] · {supplier} default is [bold]{default_account}[/bold]"
                            )
                            fix_choice = click.prompt(
                                f"  Use default ({default_account}), keep ({effective_account}), or enter a code",
                                default=effective_account,
                            ).strip()
                            if fix_choice != effective_account:
                                lines = list(patch.get("LineItems") or [{}] * len(bill.get("LineItems", [])))
                                while len(lines) <= idx:
                                    lines.append({})
                                lines[idx] = {**lines[idx], "AccountCode": fix_choice}
                                patch["LineItems"] = lines
                                console.print(f"  [green]✓ Account code → {fix_choice}[/green]")

                        if default_tax and effective_tax and effective_tax != default_tax:
                            console.print(
                                f"\n  [yellow]⚠ Tax type mismatch (line {idx}):[/yellow] "
                                f"bill has [bold]{effective_tax}[/bold] · {supplier} default is [bold]{default_tax}[/bold]"
                            )
                            fix_choice = click.prompt(
                                f"  Use default ({default_tax}), keep ({effective_tax}), or enter a type",
                                default=effective_tax,
                            ).strip()
                            if fix_choice != effective_tax:
                                lines = list(patch.get("LineItems") or [{}] * len(bill.get("LineItems", [])))
                                while len(lines) <= idx:
                                    lines.append({})
                                lines[idx] = {**lines[idx], "TaxType": fix_choice}
                                patch["LineItems"] = lines
                                console.print(f"  [green]✓ Tax type → {fix_choice}[/green]")
                except Exception:
                    pass

            console.print()
            choice = click.prompt(
                "  Action",
                type=click.Choice(["approve", "reject", "skip", "quit"]),
                default="skip",
            )

            if choice == "approve":
                if bill.get("Status") == AUTHORISED:
                    console.print("  [yellow]Bill is already AUTHORISED in Xero — marking approved in queue only.[/yellow]")
                    if not dry_run:
                        approve(item["id"])
                    approved_count += 1
                    continue

                final_patch = {**patch, "Status": AUTHORISED}

                if dry_run:
                    console.print(f"  [dim]Dry run — would apply fixes and set Status: AUTHORISED.[/dim]")
                    approved_count += 1
                else:
                    ok = push_fix(item["id"], final_patch, bill)
                    if ok:
                        approve(item["id"])
                        fix_summary = ", ".join(k for k in patch if k != "Status") or "none"
                        console.print(f"  [green]✓ Approved and authorised. Fixes applied: {fix_summary}[/green]")
                        approved_count += 1
                    else:
                        console.print("  [red]✗ Failed to update Xero — bill left unchanged. Check logs.[/red]")

            elif choice == "reject":
                if not dry_run:
                    reject(item["id"])
                console.print("  [red]✗ Rejected — bill left untouched in Xero.[/red]")
                rejected_count += 1

            elif choice == "skip":
                skipped_count += 1

            elif choice == "quit":
                console.print("[dim]  Exiting review.[/dim]")
                break

        console.print(f"\n[bold]Review complete.[/bold] Approved: {approved_count}  Rejected: {rejected_count}  Skipped: {skipped_count}")
