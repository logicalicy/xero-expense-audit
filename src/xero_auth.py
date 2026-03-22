"""
Xero OAuth 2.0 — Authorization Code flow with refresh token rotation.
One-time browser login via `python audit.py setup-auth`, then fully headless.
Refresh tokens last 60 days; rotate on every use, so stays alive indefinitely
as long as the audit runs at least once every 60 days.
"""

import os
import time
import httpx
from dotenv import load_dotenv, set_key
from pathlib import Path

load_dotenv()

TOKEN_URL = "https://identity.xero.com/connect/token"
CONNECTIONS_URL = "https://api.xero.com/connections"
ENV_PATH = Path(__file__).parent.parent / ".env"

_token_cache: dict = {}


def get_access_token() -> str:
    """
    Returns a valid access token, refreshing silently if expired.
    Saves the new refresh token back to .env after each rotation.
    """
    now = time.time()
    if _token_cache.get("expires_at", 0) > now + 30:
        return _token_cache["access_token"]

    refresh_token = os.environ.get("XERO_REFRESH_TOKEN")
    if not refresh_token:
        raise RuntimeError(
            "No XERO_REFRESH_TOKEN found in .env. Run: python audit.py setup-auth"
        )

    resp = httpx.post(
        TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
        auth=(os.environ["XERO_CLIENT_ID"], os.environ["XERO_CLIENT_SECRET"]),
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()

    _token_cache["access_token"] = data["access_token"]
    _token_cache["expires_at"] = now + data.get("expires_in", 1800)

    # Rotate refresh token — save new one to .env.
    new_refresh = data.get("refresh_token")
    if new_refresh and new_refresh != refresh_token:
        os.environ["XERO_REFRESH_TOKEN"] = new_refresh
        set_key(str(ENV_PATH), "XERO_REFRESH_TOKEN", new_refresh)

    return _token_cache["access_token"]


def exchange_code_for_tokens(code: str, redirect_uri: str) -> dict:
    """Exchange auth code for access + refresh tokens (used by setup-auth)."""
    resp = httpx.post(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
        },
        auth=(os.environ["XERO_CLIENT_ID"], os.environ["XERO_CLIENT_SECRET"]),
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def get_tenant_id(access_token: str) -> str:
    """Fetch the first connected tenant ID."""
    resp = httpx.get(
        CONNECTIONS_URL,
        headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
        timeout=15,
    )
    resp.raise_for_status()
    connections = resp.json()
    if not connections:
        raise RuntimeError("No Xero orgs connected to this app.")
    return connections[0]["tenantId"]
