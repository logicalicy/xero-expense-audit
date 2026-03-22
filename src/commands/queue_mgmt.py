"""Queue management commands — status, clear, skip, exceptions."""

from __future__ import annotations

import click
from rich.console import Console
from rich.table import Table

console = Console()


def register(cli: click.Group, load_audit_config) -> None:
    """Register queue management commands on the CLI group."""

    @cli.command()
    def status():
        """Show queue summary."""
        from ..queue import list_pending, _load

        all_items = _load()
        pending = [i for i in all_items if i["approved"] is None]
        approved = [i for i in all_items if i["approved"] is True]
        rejected = [i for i in all_items if i["approved"] is False]

        table = Table(title="Queue Status")
        table.add_column("Status")
        table.add_column("Count", justify="right")
        table.add_row("Pending review", str(len(pending)), style="yellow")
        table.add_row("Approved", str(len(approved)), style="green")
        table.add_row("Rejected", str(len(rejected)), style="red")
        table.add_row("Total", str(len(all_items)), style="bold")
        console.print(table)

    @cli.command()
    def clear():
        """Remove approved items from queue."""
        from ..queue import clear_approved
        n = clear_approved()
        console.print(f"[green]✓ Removed {n} approved item(s) from queue.[/green]")

    @cli.command()
    @click.argument("record_id")
    @click.option("--check", multiple=True, default=["MISSING_ATTACHMENT"], show_default=True,
                  help="Check codes to except (repeatable). Default: MISSING_ATTACHMENT.")
    @click.option("--reason", default="", help="Optional reason for the exception.")
    def skip(record_id: str, check: tuple, reason: str):
        """Add an exception for a record — skips specific checks in future runs.

        RECORD_ID: Xero BankTransactionID or InvoiceID.
        """
        from ..skiplist import skip_record
        from ..queue import _load as load_queue

        ref = record_id[:8]
        for item in load_queue():
            if item["id"] == record_id:
                ref = item["ref"]
                break

        checks = list(check)
        skip_record(record_id, ref, checks, reason)
        console.print(f"[green]✓ Exception added:[/green] {ref} — skipping {', '.join(checks)}")
        if reason:
            console.print(f"  Reason: {reason}")
        console.print("  Stored in [bold]audit-state.json[/bold]")

    @cli.command("exceptions")
    def list_exceptions_cmd():
        """List all manual exceptions in audit-state.json."""
        from ..skiplist import list_exceptions

        items = list_exceptions()
        if not items:
            console.print("No exceptions recorded.")
            return

        table = Table(title="Audit Exceptions")
        table.add_column("Record", style="cyan")
        table.add_column("Skipped Checks", style="yellow")
        table.add_column("Reason", style="white")
        for record_id, entry in items.items():
            checks = ", ".join(entry.get("skip_checks", []))
            reason = entry.get("reason", "—")
            ref = entry.get("ref", record_id[:8])
            table.add_row(ref, checks, reason)
        console.print(table)
