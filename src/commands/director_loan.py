"""Director loan commands — mark bills as paid and show balance summary."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, UTC

import click
import questionary
from rich.console import Console
from rich.rule import Rule
from rich.table import Table

from ..constants import AUTHORISED

console = Console()


def register(cli: click.Group, load_audit_config) -> None:
    """Register director loan commands on the CLI group."""

    @cli.command("mark-as-director-loan-payment")
    @click.option(
        "--days",
        default=int(os.environ.get("AUDIT_LOOKBACK_DAYS", 90)),
        show_default=True,
        help="Lookback window (days) to find authorised bills.",
    )
    @click.option("--dry-run", is_flag=True, help="Show what would happen without writing to Xero.")
    def mark_as_director_loan_payment(days: int, dry_run: bool):
        """Mark authorised bills as paid from the Directors Loan Account.

        Fetches AUTHORISED bills, lets you pick one or more, then posts a Xero
        Payment for each using the bill's invoice date and the Directors Loan Account.
        You'll be prompted to override the payment date before confirming.
        """
        import re as _re
        import datetime as _dt
        from ..xero_client import get as xero_get, post as xero_post

        loan_cfg = load_audit_config().get("director_loan", {})
        DIRECTORS_LOAN_CODE = loan_cfg.get("account_code", "835")
        DIRECTORS_LOAN_NAME = loan_cfg.get("account_name", "Directors Loan Account")

        def _parse_date(raw: str) -> str:
            """Convert Xero /Date(timestamp)/ to YYYY-MM-DD string."""
            if not raw:
                return ""
            m = _re.match(r"/Date\((\d+)", raw)
            if m:
                return _dt.datetime.fromtimestamp(int(m.group(1)) / 1000, tz=_dt.timezone.utc).strftime("%Y-%m-%d")
            return ""

        console.print(f"\n[bold]Fetching authorised bills (last {days} days)...[/bold]")
        since = (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%d")
        xero_date = since.replace("-", ",")

        try:
            data = xero_get(
                "Invoices",
                params={
                    "where": f'Type=="ACCPAY" AND Date>=DateTime({xero_date}) AND Status=="{AUTHORISED}"',
                    "order": "Date DESC",
                    "page": 1,
                    "unitdp": 4,
                },
            )
        except Exception as exc:
            console.print(f"[red]Failed to fetch bills: {exc}[/red]")
            return

        bills = data.get("Invoices", [])
        if not bills:
            console.print("[yellow]No authorised bills found in the last {days} days.[/yellow]")
            return

        choices = []
        for idx, b in enumerate(bills, 1):
            date_str = _parse_date(b.get("Date", "")) or "—"
            supplier = (b.get("Contact", {}).get("Name", "—") or "—")[:30]
            currency = b.get("CurrencyCode", "")
            amount_due = b.get("AmountDue", b.get("Total", ""))
            ref = (b.get("InvoiceNumber") or b.get("Reference") or "")[:24]
            label = f"{idx:>2}.  {date_str}  {supplier:<30}  {currency} {amount_due:<10}  {ref}"
            choices.append(questionary.Choice(title=label, value=idx - 1))

        selected_indices = questionary.checkbox(
            "Select bills to mark as paid (↑↓ navigate, Space toggle, Enter confirm):",
            choices=choices,
        ).ask()

        if selected_indices is None:
            return
        if not selected_indices:
            console.print("[yellow]No bills selected — nothing to do.[/yellow]")
            return

        toggles: set[int] = set(selected_indices)
        selected_bills = [bills[i] for i in sorted(toggles)]

        console.print(f"\n[bold]{len(selected_bills)} bill(s) selected:[/bold]")

        bill_payment_dates: dict[str, str] = {}
        total_gbp = 0.0

        for b in selected_bills:
            invoice_id = b["InvoiceID"]
            invoice_date = _parse_date(b.get("Date", "")) or "unknown"
            bill_payment_dates[invoice_id] = invoice_date

            supplier = b.get("Contact", {}).get("Name", "—")[:36]
            currency = b.get("CurrencyCode", "GBP")
            amount_due = float(b.get("AmountDue") or b.get("Total") or 0)
            if currency == "GBP":
                total_gbp += amount_due
            console.print(f"  • {supplier:<36}  {currency} {amount_due:>10.2f}  date: {invoice_date}")

        if total_gbp:
            console.print(f"\n  [bold]GBP total: £{total_gbp:,.2f}[/bold]")

        console.print(f"  Payment account: [cyan]{DIRECTORS_LOAN_CODE} — {DIRECTORS_LOAN_NAME}[/cyan]")
        console.print("  Payment dates: each bill's invoice date (shown above)")

        override = click.prompt(
            "\n  Override payment date for all selected bills? (YYYY-MM-DD, or leave blank to keep per-bill dates)",
            default="",
            show_default=False,
        ).strip()

        if override:
            try:
                _dt.datetime.strptime(override, "%Y-%m-%d")
                for invoice_id in bill_payment_dates:
                    bill_payment_dates[invoice_id] = override
                console.print(f"  [green]✓ All payments will be dated {override}[/green]")
            except ValueError:
                console.print("  [yellow]Invalid date format (expected YYYY-MM-DD) — keeping per-bill invoice dates.[/yellow]")

        console.print()
        if dry_run:
            console.print("[dim]Dry run — nothing will be written to Xero.[/dim]")

        confirm = click.prompt(
            f"  Mark {len(selected_bills)} bill(s) as paid from {DIRECTORS_LOAN_CODE} - {DIRECTORS_LOAN_NAME}? [y/n]",
            default="n",
        ).strip().lower()

        if confirm not in ("y", "yes"):
            console.print("  Aborted.")
            return

        console.print()
        success_count = 0
        failed_count = 0

        for b in selected_bills:
            invoice_id = b["InvoiceID"]
            supplier = b.get("Contact", {}).get("Name", "—")
            currency = b.get("CurrencyCode", "GBP")
            amount_due = float(b.get("AmountDue") or b.get("Total") or 0)
            payment_date = bill_payment_dates[invoice_id]

            if not payment_date or payment_date == "unknown":
                console.print(f"  [red]✗[/red] {supplier} — cannot determine payment date, skipping.")
                failed_count += 1
                continue

            if dry_run:
                console.print(f"  [dim]Dry run — would pay: {supplier} {currency} {amount_due:.2f} on {payment_date} from {DIRECTORS_LOAN_CODE}[/dim]")
                success_count += 1
                continue

            try:
                xero_post("Payments", {
                    "Invoice": {"InvoiceID": invoice_id},
                    "Account": {"Code": DIRECTORS_LOAN_CODE},
                    "Date": payment_date,
                    "Amount": amount_due,
                })
                console.print(f"  [green]✓[/green] {supplier} — {currency} {amount_due:.2f} paid on {payment_date}")
                success_count += 1
            except Exception as exc:
                console.print(f"  [red]✗[/red] {supplier} — failed: {exc}")
                failed_count += 1

        console.print()
        if failed_count:
            console.print(f"[yellow]Done. {success_count} paid, {failed_count} failed.[/yellow]")
        else:
            console.print(f"[green]✅ {success_count} bill(s) marked as paid from {DIRECTORS_LOAN_CODE} — {DIRECTORS_LOAN_NAME}.[/green]")

    @cli.command("director-loan-balance")
    @click.option("--days", default=365, show_default=True, help="Days back to look for director loan payments.")
    def loan_balance(days: int):
        """Show Directors Loan Account balance — payments made vs. bills still outstanding."""
        import re as _re
        import datetime as _dt
        from ..xero_client import get as xero_get

        loan_cfg = load_audit_config().get("director_loan", {})
        DIRECTORS_LOAN_CODE = loan_cfg.get("account_code", "835")
        DIRECTORS_LOAN_NAME = loan_cfg.get("account_name", "Directors Loan Account")

        def _parse_date(raw: str) -> str:
            if not raw:
                return ""
            m = _re.match(r"/Date\((\d+)", raw)
            if m:
                return _dt.datetime.fromtimestamp(int(m.group(1)) / 1000, tz=_dt.timezone.utc).strftime("%Y-%m-%d")
            return ""

        def _amount(val) -> float:
            try:
                return float(val or 0)
            except (TypeError, ValueError):
                return 0.0

        console.print()

        console.print(f"[bold]Fetching payments from {DIRECTORS_LOAN_CODE} – {DIRECTORS_LOAN_NAME}...[/bold]")
        since = (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%d")
        xero_date = since.replace("-", ",")

        try:
            payments_data = xero_get(
                "Payments",
                params={
                    "where": f'Account.Code=="{DIRECTORS_LOAN_CODE}" AND Date>=DateTime({xero_date}) AND Status=="{AUTHORISED}"',
                    "order": "Date DESC",
                },
            )
        except Exception as exc:
            console.print(f"[red]Failed to fetch payments: {exc}[/red]")
            return

        payments = payments_data.get("Payments", [])
        paid_total = sum(_amount(p.get("Amount")) for p in payments)

        console.print("[bold]Fetching outstanding authorised bills...[/bold]")
        try:
            bills_data = xero_get(
                "Invoices",
                params={
                    "where": f'Type=="ACCPAY" AND Status=="{AUTHORISED}"',
                    "order": "Date DESC",
                    "unitdp": 4,
                },
            )
        except Exception as exc:
            console.print(f"[red]Failed to fetch bills: {exc}[/red]")
            return

        outstanding_bills = [b for b in bills_data.get("Invoices", []) if _amount(b.get("AmountDue")) > 0]
        outstanding_total = sum(_amount(b.get("AmountDue")) for b in outstanding_bills)

        console.print()

        if payments:
            pay_table = Table(show_header=True, header_style="bold cyan", box=None, padding=(0, 1))
            pay_table.add_column("Date", width=12)
            pay_table.add_column("Supplier", min_width=28)
            pay_table.add_column("Amount", justify="right", width=12)
            for p in payments:
                invoice = (p.get("Invoice") or {})
                supplier = (invoice.get("Contact") or {}).get("Name") or "—"
                pay_table.add_row(
                    _parse_date(p.get("Date", "")) or "—",
                    supplier[:40],
                    f"£{_amount(p.get('Amount')):,.2f}",
                )
            console.print(f"[bold green]Payments from {DIRECTORS_LOAN_CODE} (last {days} days)[/bold green]")
            console.print(pay_table)
            console.print(f"  [bold]Total paid:          £{paid_total:>10,.2f}[/bold]\n")
        else:
            console.print(f"[dim]No payments posted from {DIRECTORS_LOAN_CODE} in the last {days} days.[/dim]\n")

        if outstanding_bills:
            bill_table = Table(show_header=True, header_style="bold cyan", box=None, padding=(0, 1))
            bill_table.add_column("Date", width=12)
            bill_table.add_column("Supplier", min_width=28)
            bill_table.add_column("Amount due", justify="right", width=12)
            bill_table.add_column("Ref", width=24)
            for b in outstanding_bills:
                bill_table.add_row(
                    _parse_date(b.get("Date", "")) or "—",
                    (b.get("Contact", {}).get("Name", "—") or "—")[:40],
                    f"£{_amount(b.get('AmountDue')):,.2f}",
                    (b.get("InvoiceNumber") or b.get("Reference") or "")[:24],
                )
            console.print("[bold yellow]Outstanding authorised bills (unpaid)[/bold yellow]")
            console.print(bill_table)
            console.print(f"  [bold]Total outstanding:   £{outstanding_total:>10,.2f}[/bold]\n")
        else:
            console.print("[green]No outstanding authorised bills.[/green]\n")

        console.print(Rule(style="dim"))
        console.print(f"  Paid from {DIRECTORS_LOAN_CODE} (last {days}d):    £{paid_total:>10,.2f}")
        console.print(f"  Still outstanding:              £{outstanding_total:>10,.2f}")
        net = paid_total - outstanding_total
        sign = "+" if net >= 0 else ""
        colour = "green" if net >= 0 else "yellow"
        console.print(f"  [bold {colour}]Net:[/bold {colour}]                            [{colour}]£{sign}{net:>10,.2f}[/{colour}]")
        console.print()
