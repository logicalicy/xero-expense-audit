"""Setup command — one-time Xero OAuth 2.0 browser login."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import click
from rich.console import Console

console = Console()


def register(cli: click.Group, load_audit_config) -> None:
    """Register the setup-auth command on the CLI group."""

    @cli.command("setup-auth")
    def setup_auth():
        """One-time browser login — saves refresh token + tenant ID to .env."""
        import urllib.parse
        import webbrowser
        from http.server import HTTPServer, BaseHTTPRequestHandler
        from ..xero_auth import exchange_code_for_tokens, get_tenant_id
        from dotenv import set_key

        client_id = os.environ.get("XERO_CLIENT_ID")
        client_secret = os.environ.get("XERO_CLIENT_SECRET")
        if not client_id or not client_secret:
            console.print("[red]XERO_CLIENT_ID and XERO_CLIENT_SECRET must be set in .env first.[/red]")
            sys.exit(1)

        REDIRECT_URI = "http://localhost:8080/callback"
        SCOPES = " ".join([
            "accounting.banktransactions",
            "accounting.invoices",
            "accounting.payments",
            "accounting.contacts",
            "accounting.settings",
            "accounting.attachments",
            "openid",
            "profile",
            "email",
            "offline_access",
        ])

        auth_url = (
            "https://login.xero.com/identity/connect/authorize?"
            + urllib.parse.urlencode({
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": REDIRECT_URI,
                "scope": SCOPES,
                "state": "xero-expense-audit",
            })
        )

        result: dict = {}

        class CallbackHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                parsed = urllib.parse.urlparse(self.path)
                params = urllib.parse.parse_qs(parsed.query)
                code = params.get("code", [None])[0]
                if code:
                    result["code"] = code
                    self.send_response(200)
                    self.send_header("Content-type", "text/html")
                    self.end_headers()
                    self.wfile.write(b"<h2>Auth complete. You can close this tab.</h2>")
                else:
                    self.send_response(400)
                    self.end_headers()
                    self.wfile.write(b"<h2>No code received. Something went wrong.</h2>")

            def log_message(self, *args):
                pass

        server = HTTPServer(("localhost", 8080), CallbackHandler)

        console.print("\n[bold]Xero OAuth Setup[/bold]")
        console.print("Opening browser for Xero login...")
        console.print(f"  Redirect URI: [cyan]{REDIRECT_URI}[/cyan]")
        console.print("  (Make sure this is added to your app's redirect URIs in developer.xero.com)\n")

        webbrowser.open(auth_url)

        console.print("Waiting for callback on localhost:8080...")
        server.handle_request()

        if "code" not in result:
            console.print("[red]No auth code received. Aborting.[/red]")
            sys.exit(1)

        console.print("  ✓ Got auth code. Exchanging for tokens...")
        tokens = exchange_code_for_tokens(result["code"], REDIRECT_URI)

        access_token = tokens["access_token"]
        refresh_token = tokens["refresh_token"]

        console.print("  ✓ Got tokens. Fetching tenant ID...")
        tenant_id = get_tenant_id(access_token)

        env_path = str(Path(__file__).parent.parent.parent / ".env")
        if not Path(env_path).exists():
            Path(env_path).write_text("")

        set_key(env_path, "XERO_REFRESH_TOKEN", refresh_token)
        set_key(env_path, "XERO_TENANT_ID", tenant_id)

        console.print(f"\n[green]✅ Auth complete![/green]")
        console.print(f"  Tenant ID:     [cyan]{tenant_id}[/cyan]")
        console.print(f"  Refresh token: saved to .env")
        console.print(f"\nRun [bold]python audit.py run[/bold] when ready.")
