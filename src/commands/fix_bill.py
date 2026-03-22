"""Fix commands — interactive bill correction and auto-fix loops."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, UTC

import click
from rich.console import Console

from ..constants import DRAFT, SUBMITTED

console = Console()


def _offer_submit(invoice_id: str, bill: dict, dry_run: bool) -> None:
    """Offer to advance a clean/fixed DRAFT bill to SUBMITTED so it leaves the queue."""
    if bill.get("Status") != DRAFT:
        return
    try:
        total = float(bill.get("Total") or 0)
    except (TypeError, ValueError):
        total = 0.0
    if total == 0.0:
        console.print("  [red]⚠ Bill total is £0.00 — not submitting until amount is set.[/red]")
        return
    if dry_run:
        console.print("  [dim]Dry run — would offer to submit for approval.[/dim]")
        return
    answer = click.prompt(
        "  Submit for approval? Moves bill out of DRAFT so it won't reappear [y/n]",
        default="y",
    ).strip().lower()
    if answer in ("y", "yes", ""):
        from ..xero_client import post
        try:
            post(f"Invoices/{invoice_id}", {"InvoiceID": invoice_id, "Status": SUBMITTED})
            console.print("  [green]✓ Submitted for approval.[/green]")
        except Exception as exc:
            console.print(f"  [yellow]Could not submit: {exc}[/yellow]")


def _pick_bill_interactively(
    days: int = int(os.environ.get("AUDIT_LOOKBACK_DAYS", 90))
) -> str | None:
    """Fetch recent DRAFT bills and let the user pick one."""
    from ..xero_client import get as xero_get

    console.print(f"\n[bold]Fetching recent bills (last {days} days)...[/bold]")
    since = (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%d")
    xero_date = since.replace("-", ",")
    try:
        data = xero_get(
            "Invoices",
            params={
                "where": f'Type=="ACCPAY" AND Date>=DateTime({xero_date}) AND Status=="{DRAFT}"',
                "order": "Date DESC",
                "page": 1,
                "unitdp": 4,
            },
        )
    except Exception as exc:
        console.print(f"[red]Failed to fetch bills: {exc}[/red]")
        return None

    bills = data.get("Invoices", [])
    if not bills:
        console.print("[yellow]No recent bills found.[/yellow]")
        return None

    from rich.table import Table
    table = Table(show_header=True, header_style="bold cyan", box=None, padding=(0, 1))
    table.add_column("#", style="dim", width=4)
    table.add_column("Date", width=12)
    table.add_column("Supplier", min_width=20)
    table.add_column("Amount", justify="right", width=14)
    table.add_column("Status", width=20)
    table.add_column("ID", style="dim", width=10)

    for idx, b in enumerate(bills, 1):
        date_raw = b.get("Date", "")
        date_str = "—"
        if date_raw:
            import re
            m = re.match(r"/Date\((\d+)", date_raw)
            if m:
                from datetime import timezone
                date_str = datetime.fromtimestamp(int(m.group(1)) / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        supplier = b.get("Contact", {}).get("Name", "—")[:32]
        currency = b.get("CurrencyCode", "")
        total = b.get("Total", "")
        status = b.get("Status", "")
        bid = b.get("InvoiceID", "")
        table.add_row(str(idx), date_str, supplier, f"{currency} {total}", status, bid[:8] + "…")

    console.print(table)
    console.print()

    while True:
        raw = click.prompt(f"Pick a bill [1-{len(bills)}] or 'q' to quit", default="q").strip()
        if raw.lower() == "q":
            return None
        if raw.isdigit() and 1 <= int(raw) <= len(bills):
            return bills[int(raw) - 1]["InvoiceID"]
        console.print(f"[yellow]Enter a number between 1 and {len(bills)}, or 'q'.[/yellow]")


def register(cli: click.Group, load_audit_config) -> None:
    """Register fix commands on the CLI group."""

    @cli.command("fix-next-bill")
    @click.option(
        "--days",
        default=int(os.environ.get("AUDIT_LOOKBACK_DAYS", 90)),
        show_default=True,
        help="Lookback window (days) to find DRAFT bills.",
    )
    @click.option("--dry-run", is_flag=True, help="Show what would be fixed without writing to Xero.")
    @click.option("--verbose", "-v", is_flag=True, help="Show strategy debug output.")
    @click.pass_context
    def fix_next_bill(ctx: click.Context, days: int, dry_run: bool, verbose: bool):
        """Pick the oldest DRAFT bill and run fix-bill on it. Loops until you say stop."""
        from ..xero_client import get as xero_get
        import re as _re, datetime as _dt

        since = (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%d")
        xero_date = since.replace("-", ",")

        processed: set[str] = set()

        while True:
            try:
                data = xero_get(
                    "Invoices",
                    params={
                        "where": f'Type=="ACCPAY" AND Date>=DateTime({xero_date}) AND Status=="{DRAFT}"',
                        "order": "Date ASC",
                        "page": 1,
                        "unitdp": 4,
                    },
                )
            except Exception as exc:
                console.print(f"[red]Failed to fetch bills: {exc}[/red]")
                return

            bills = [b for b in data.get("Invoices", []) if b["InvoiceID"] not in processed]
            if not bills:
                console.print(f"[green]✅ No more DRAFT bills to fix.[/green]")
                return

            next_bill = bills[0]
            invoice_id = next_bill["InvoiceID"]
            supplier = next_bill.get("Contact", {}).get("Name", "—")
            date_raw = next_bill.get("Date", "")
            date_str = "—"
            m = _re.match(r"/Date\((\d+)", date_raw)
            if m:
                date_str = _dt.datetime.fromtimestamp(int(m.group(1)) / 1000, tz=_dt.timezone.utc).strftime("%Y-%m-%d")

            xero_url = f"https://go.xero.com/AccountsPayable/Edit.aspx?InvoiceID={invoice_id}"
            console.print(f"\n  Next bill: [cyan]{supplier}[/cyan]  {date_str}  [dim]{invoice_id[:8]}…[/dim]")
            console.print(f"  [dim]{xero_url}[/dim]")
            ctx.invoke(fix_bill, invoice_id=invoice_id, days=days, dry_run=dry_run, verbose=verbose)
            processed.add(invoice_id)

            cont = click.prompt("\n  Continue to next bill? [y/n]", default="y").strip().lower()
            if cont not in ("y", "yes", ""):
                console.print("  Done.")
                return

    @cli.command("fix-bill")
    @click.argument("invoice_id", required=False, default=None)
    @click.option(
        "--days",
        default=int(os.environ.get("AUDIT_LOOKBACK_DAYS", 90)),
        show_default=True,
        help="Lookback window (days) to find bills for the picker.",
    )
    @click.option("--dry-run", is_flag=True, help="Show what would be fixed without writing to Xero.")
    @click.option("--verbose", "-v", is_flag=True, help="Show strategy debug output.")
    def fix_bill(invoice_id: str | None, days: int, dry_run: bool, verbose: bool):
        """Fetch a bill, validate it, and apply all available auto-corrections.

        INVOICE_ID: Xero InvoiceID (from the URL: ?InvoiceID=...).
        If omitted, shows an interactive picker of recent bills.
        """
        if verbose:
            logging.basicConfig(level=logging.INFO, format="  [dim]%(name)s: %(message)s[/dim]")

        from ..xero_client import get
        from ..reference_data_loader import load_reference_data
        from ..validators.bills import validate_bills
        from ..fix_bills import apply_fixes, push_fix

        config = load_audit_config()

        if not invoice_id:
            invoice_id = _pick_bill_interactively(days=days)
            if not invoice_id:
                return

        console.print(f"\n[bold]Fetching bill {invoice_id[:8]}...[/bold]")
        data = get(f"Invoices/{invoice_id}", params={"unitdp": 4})
        invoices = data.get("Invoices", [])
        if not invoices:
            console.print(f"[red]Bill not found: {invoice_id}[/red]")
            return
        bill = invoices[0]

        supplier = bill.get("Contact", {}).get("Name", "—")
        total = bill.get("Total", "")
        currency = bill.get("CurrencyCode", "GBP")
        status = bill.get("Status", "")
        amount_label = f"{currency} {total}"
        if bill.get("HasAttachments") and currency == "GBP":
            amount_label += " [dim](as entered — may be corrected after FX check)[/dim]"
        console.print(f"  Supplier: [cyan]{supplier}[/cyan]  Amount: {amount_label}  Status: {status}")

        console.print("  Loading reference data...")
        ref = load_reference_data()

        console.print("  Validating...")
        results = validate_bills([bill], ref)
        if not results:
            console.print("[green]✅ No issues found — bill looks clean.[/green]")
            _offer_submit(invoice_id, bill, dry_run)
            return

        result = results[0]
        console.print(f"\n  Issues found ({len(result.issues)}):")
        for issue in result.issues:
            console.print(f"    [yellow]•[/yellow] {issue.code}: {issue.message}")

        console.print("\n  Computing auto-corrections...")
        patch = apply_fixes(bill, result.issues, ref, config)

        # FOREIGN_CURRENCY_AMOUNT with _fx_verified means the strategy confirmed GBP.
        if patch.get("_fx_verified"):
            result.issues = [i for i in result.issues if i.code != "FOREIGN_CURRENCY_AMOUNT"]
            patch.pop("_fx_verified")
            real_patch = {k: v for k, v in patch.items() if not k.startswith("_")}
            if not result.issues and not real_patch:
                console.print("[green]✅ Bill looks clean — attachment confirmed GBP, no issues.[/green]")
                _offer_submit(invoice_id, bill, dry_run)
                return
            if real_patch:
                console.print("  [dim](FX: attachment confirmed GBP — correcting tax type)[/dim]")
            elif not result.issues:
                console.print("  [dim](FX: attachment confirmed GBP — no conversion needed)[/dim]")

        # Interactive fallback for MISSING_ACCOUNT_CODE when supplier has no default.
        missing_account_issues = [i for i in result.issues if i.code == "MISSING_ACCOUNT_CODE"]
        supplier_still_missing = any(i.code == "MISSING_SUPPLIER" for i in result.issues) and "_inferred_supplier_name" in patch
        if missing_account_issues and not supplier_still_missing:
            patch_lines = patch.get("LineItems", [{}] * len(bill.get("LineItems", [])))
            needs_account = any(
                not pl.get("AccountCode") and not bill.get("LineItems", [{}])[i].get("AccountCode")
                for i, pl in enumerate(patch_lines)
            )
            if needs_account:
                accounts_sorted = sorted(
                    [a for a in ref.accounts.values() if a.get("Type") in ("EXPENSE", "OVERHEADS", "DIRECTCOSTS")],
                    key=lambda a: a.get("Code", ""),
                )
                PAGE = 15
                page_start = 0
                chosen_code = None
                console.print(f"\n  [yellow]No default account code for {supplier}.[/yellow]")
                console.print("  [dim]Enter # or code to pick · Enter to see more · s to skip[/dim]")
                while chosen_code is None:
                    page = accounts_sorted[page_start:page_start + PAGE]
                    for i, acct in enumerate(page, page_start + 1):
                        console.print(f"    [{i}] {acct['Code']} — {acct['Name']}")
                    has_more = page_start + PAGE < len(accounts_sorted)
                    prompt_hint = "more/code/#/s" if has_more else "code/#/s"
                    choice = click.prompt(f"  [{prompt_hint}]", default="", show_default=False).strip()
                    if choice == "" and has_more:
                        page_start += PAGE
                        continue
                    if choice.lower() == "s" or (choice == "" and not has_more):
                        break
                    if choice.isdigit() and 1 <= int(choice) <= len(accounts_sorted):
                        chosen_code = accounts_sorted[int(choice) - 1]["Code"]
                    elif choice in ref.accounts:
                        chosen_code = choice
                    else:
                        console.print(f"  [yellow]'{choice}' not recognised — try again.[/yellow]")
                if chosen_code:
                    lines = list(patch.get("LineItems", [{}] * len(bill.get("LineItems", []))))
                    while len(lines) < len(bill.get("LineItems", [])):
                        lines.append({})
                    for i, orig_line in enumerate(bill.get("LineItems", [])):
                        if not lines[i].get("AccountCode") and not orig_line.get("AccountCode"):
                            lines[i] = {**lines[i], "AccountCode": chosen_code}
                    patch["LineItems"] = lines

                    contact_id = bill.get("Contact", {}).get("ContactID")
                    if contact_id and not dry_run:
                        save_default = click.prompt(
                            f"  Save {chosen_code} as default account for {supplier}? [y/n]",
                            default="y",
                        ).strip().lower()
                        if save_default in ("y", "yes", ""):
                            from ..xero_client import post
                            try:
                                post("Contacts", {"Contacts": [{"ContactID": contact_id, "PurchasesDefaultAccountCode": chosen_code}]})
                                console.print(f"  [green]✓ {supplier} default account → {chosen_code}[/green]")
                            except Exception as exc:
                                console.print(f"  [yellow]Could not update contact: {exc}[/yellow]")

        # Handle MISSING_SUPPLIER interactively if AI only got a name but no contact match.
        if "_inferred_supplier_name" in patch:
            raw_name = patch.pop("_inferred_supplier_name")
            inferred_name = raw_name.strip()
            console.print(f"\n  [yellow]Supplier not found.[/yellow] AI inferred name from receipt: [cyan]{inferred_name}[/cyan]")
            console.print("  Existing suppliers:")
            suppliers = list(ref.suppliers.values())[:20]
            for i, s in enumerate(suppliers, 1):
                console.print(f"    [{i}] {s['Name']}")
            create_idx = len(suppliers) + 1
            console.print(f"    [{create_idx}] Create new contact: \"{inferred_name}\"")
            choice = click.prompt("  Select supplier", default=str(create_idx))
            if choice.isdigit() and 1 <= int(choice) <= len(suppliers):
                contact = suppliers[int(choice) - 1]
                patch["Contact"] = {"ContactID": contact["ContactID"], "Name": contact["Name"]}
                console.print(f"  Selected: [green]{contact['Name']}[/green]")
            elif choice == str(create_idx) or choice == "n":
                nc_defaults = config.get("new_contact_defaults", {})
                default_account_name = nc_defaults.get("purchases_account_name", "")
                default_tax_type = nc_defaults.get("purchases_tax_type", "")

                default_account_code = ""
                if default_account_name:
                    for code, acct in ref.accounts.items():
                        if acct.get("Name", "").lower() == default_account_name.lower():
                            default_account_code = code
                            break

                resolved_tax_type = ""
                if default_tax_type:
                    for name in ref.tax_rates:
                        if name.lower() == default_tax_type.lower():
                            resolved_tax_type = ref.tax_rates[name].get("TaxType", "")
                            break

                confirmed_name = click.prompt("  Contact name", default=inferred_name)
                account_number = click.prompt("  Account number (optional)", default="", show_default=False)
                new_contact_payload: dict = {"Name": confirmed_name}
                if account_number.strip():
                    new_contact_payload["AccountNumber"] = account_number.strip()
                if default_account_code:
                    new_contact_payload["PurchasesDefaultAccountCode"] = default_account_code
                if resolved_tax_type:
                    new_contact_payload["PurchasesDefaultTaxType"] = resolved_tax_type
                parts = [f"[green]{confirmed_name}[/green]"]
                if default_account_code:
                    parts.append(f"account={default_account_code} ({default_account_name})")
                if resolved_tax_type:
                    parts.append(f"tax={resolved_tax_type}")
                if dry_run:
                    console.print(f"  [dim]Dry run — would create contact: {' · '.join(parts)}[/dim]")
                    patch["Contact"] = {"ContactID": "DRY-RUN", "Name": confirmed_name}
                    if default_account_code or resolved_tax_type:
                        patched_lines = list(patch.get("LineItems", []))
                        orig_lines = bill.get("LineItems", [])
                        while len(patched_lines) < len(orig_lines):
                            patched_lines.append({})
                        for i, orig in enumerate(orig_lines):
                            merged = dict(patched_lines[i])
                            if default_account_code and not merged.get("AccountCode") and not orig.get("AccountCode"):
                                merged["AccountCode"] = default_account_code
                            if resolved_tax_type and not merged.get("TaxType") and not orig.get("TaxType"):
                                merged["TaxType"] = resolved_tax_type
                            patched_lines[i] = merged
                        patch["LineItems"] = patched_lines
                else:
                    from ..xero_client import post
                    new_contact = post("Contacts", {"Contacts": [new_contact_payload]})
                    created = new_contact.get("Contacts", [{}])[0]
                    patch["Contact"] = {"ContactID": created["ContactID"], "Name": created["Name"]}
                    console.print(f"  Created new contact: {' · '.join(parts)}")

                    if default_account_code or resolved_tax_type:
                        patched_lines = list(patch.get("LineItems", []))
                        orig_lines = bill.get("LineItems", [])
                        while len(patched_lines) < len(orig_lines):
                            patched_lines.append({})
                        for i, orig in enumerate(orig_lines):
                            merged = dict(patched_lines[i])
                            if default_account_code and not merged.get("AccountCode") and not orig.get("AccountCode"):
                                merged["AccountCode"] = default_account_code
                            if resolved_tax_type and not merged.get("TaxType") and not orig.get("TaxType"):
                                merged["TaxType"] = resolved_tax_type
                            patched_lines[i] = merged
                        patch["LineItems"] = patched_lines
            else:
                console.print("  Skipping supplier assignment.")

        if not patch:
            console.print("[yellow]  No auto-corrections available for these issues.[/yellow]")
            return

        def _fmt(val: object) -> str:
            """Human-readable value for display."""
            import re, datetime
            if isinstance(val, str):
                m = re.match(r"/Date\((\d+)([+-]\d{4})?\)/", val)
                if m:
                    ts = int(m.group(1)) / 1000
                    return datetime.datetime.fromtimestamp(ts, datetime.UTC).strftime("%Y-%m-%d")
            if isinstance(val, dict):
                def _strip(d: object) -> object:
                    if isinstance(d, dict):
                        return {k: _strip(v) for k, v in d.items()
                                if v not in ("", [], {}, None)}
                    if isinstance(d, list):
                        return [_strip(i) for i in d if i not in ("", [], {}, None)]
                    return d
                cleaned = _strip(val)
                return str(cleaned)
            return repr(val)

        if dry_run:
            console.print("\n[dim]Dry run — nothing written to Xero.[/dim]")
            return

        def _show_patch(p: dict) -> None:
            console.print("\n  Corrections to apply:")
            if "_fx_note" in p:
                console.print(f"    [cyan]FX:[/cyan] {p['_fx_note']}")
            if "_post_write_note" in p:
                console.print(f"    [cyan]Xero history note:[/cyan] {p['_post_write_note']}")
            if "_post_write_csv" in p:
                csv_filename = p["_post_write_csv"][0]
                console.print(f"    [cyan]ECB evidence attachment:[/cyan] {csv_filename}")
            for field, value in p.items():
                if field.startswith("_"):
                    continue
                if field == "LineItems":
                    for i, line in enumerate(value):
                        for k, v in line.items():
                            orig = bill.get("LineItems", [{}])[i].get(k, "") if i < len(bill.get("LineItems", [])) else ""
                            if v != orig:
                                console.print(f"    [green]LineItems[{i}].{k}:[/green] {_fmt(orig)} → {_fmt(v)}")
                else:
                    orig = bill.get(field, "")
                    console.print(f"    [green]{field}:[/green] {_fmt(orig)} → {_fmt(value)}")

        def _merge_patch(base: dict, delta: dict) -> dict:
            """Deep-merge delta into base. LineItems merged by index."""
            result = dict(base)
            for key, val in delta.items():
                if key == "LineItems" and "LineItems" in result:
                    base_lines = list(result["LineItems"])
                    delta_lines = val if isinstance(val, list) else []
                    merged = []
                    for i in range(max(len(base_lines), len(delta_lines))):
                        if i < len(base_lines) and i < len(delta_lines):
                            merged.append({**base_lines[i], **delta_lines[i]})
                        elif i < len(base_lines):
                            merged.append(base_lines[i])
                        else:
                            merged.append(delta_lines[i])
                    result["LineItems"] = merged
                else:
                    result[key] = val
            return result

        def _apply_instruction(instruction: str, p: dict) -> dict:
            """Use AI to interpret a natural-language edit instruction and return updated patch."""
            import anthropic as _anthropic
            client = _anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
            current_patch_str = json.dumps(p, indent=2, default=str)
            bill_summary = json.dumps({
                "Contact": bill.get("Contact", {}).get("Name"),
                "LineItems": [{"Description": l.get("Description"), "AccountCode": l.get("AccountCode"), "TaxType": l.get("TaxType")} for l in bill.get("LineItems", [])],
                "DueDate": bill.get("DueDate"),
                "Reference": bill.get("Reference"),
            }, indent=2, default=str)
            msg = client.messages.create(
                model=_text_model(),
                max_tokens=512,
                system="""You are a Xero bill patch editor. Given a current patch dict and a user instruction, return ONLY the fields that need to change as a valid JSON dict (a delta patch).
Xero field names: Contact (dict with ContactID+Name), LineItems (list of dicts with Description, AccountCode, TaxType, Quantity, UnitAmount), DueDate, Reference.
For line item fields use {"LineItems": [{"Description": "..."}]} — index 0 is the first line item.
Return ONLY the changed fields as valid JSON. Do NOT repeat unchanged fields. No explanation.""",
                messages=[{"role": "user", "content": f"Current patch:\n{current_patch_str}\n\nBill context:\n{bill_summary}\n\nInstruction: {instruction}"}],
            )
            from ..fix_bills import _extract_json
            raw = _extract_json(msg.content[0].text)
            delta = json.loads(raw)
            return _merge_patch(p, delta)

        from ..fix_bills import get_text_model as _text_model

        _show_patch(patch)

        console.print("\n  [bold]Proceed?[/bold] Press [green]y[/green] to apply, [red]n[/red] to abort, or describe a change:")
        while True:
            user_input = click.prompt("  >", default="y", prompt_suffix=" ").strip()

            if user_input.lower() in ("y", "yes", ""):
                ok = push_fix(invoice_id, patch, bill)
                if ok:
                    console.print(f"[green]✅ Bill {invoice_id[:8]} updated in Xero.[/green]")
                    console.print(f"  → https://go.xero.com/AccountsPayable/Edit.aspx?InvoiceID={invoice_id}")
                    _offer_submit(invoice_id, bill, dry_run)
                else:
                    console.print("[red]✗ Write to Xero failed — check logs.[/red]")
                break

            elif user_input.lower() in ("n", "no", "abort", "cancel"):
                console.print("  Aborted.")
                break

            else:
                console.print("  [dim]Applying change...[/dim]")
                try:
                    patch = _apply_instruction(user_input, patch)
                    _show_patch(patch)
                    console.print("\n  Anything else, or [green]y[/green] to apply / [red]n[/red] to abort:")
                except Exception as e:
                    console.print(f"  [red]Couldn't parse that instruction: {e}[/red] — try again or type y/n.")
