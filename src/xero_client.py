"""
Xero API client — thin wrapper around httpx.
Handles auth headers, tenant ID, and pagination.
"""

import os
from typing import Any, Generator
import httpx
from dotenv import load_dotenv
from .xero_auth import get_access_token

load_dotenv()

BASE_URL = "https://api.xero.com/api.xro/2.0"


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {get_access_token()}",
        "Xero-Tenant-Id": os.environ["XERO_TENANT_ID"],
        "Accept": "application/json",
    }


def get(endpoint: str, params: dict | None = None) -> dict:
    url = f"{BASE_URL}/{endpoint.lstrip('/')}"
    resp = httpx.get(url, headers=_headers(), params=params or {}, timeout=30)
    resp.raise_for_status()
    return resp.json()


def get_bytes(endpoint: str, params: dict | None = None) -> tuple[bytes, str]:
    """Like get() but returns (raw_bytes, content_type) instead of parsed JSON."""
    url = f"{BASE_URL}/{endpoint.lstrip('/')}"
    resp = httpx.get(url, headers=_headers(), params=params or {}, timeout=30)
    resp.raise_for_status()
    return resp.content, resp.headers.get("content-type", "")


def put_bytes(endpoint: str, data: bytes, content_type: str, filename: str) -> dict:
    """PUT raw bytes to Xero (used for attachment uploads)."""
    url = f"{BASE_URL}/{endpoint.lstrip('/')}"
    headers = {
        **_headers(),
        "Content-Type": content_type,
        "x-filename": filename,
    }
    resp = httpx.put(url, headers=headers, content=data, timeout=60)
    resp.raise_for_status()
    return resp.json()


def put(endpoint: str, payload: dict) -> dict:
    url = f"{BASE_URL}/{endpoint.lstrip('/')}"
    resp = httpx.put(url, headers={**_headers(), "Content-Type": "application/json"}, json=payload, timeout=30)
    resp.raise_for_status()
    return resp.json()


def post(endpoint: str, payload: dict) -> dict:
    url = f"{BASE_URL}/{endpoint.lstrip('/')}"
    resp = httpx.post(url, headers={**_headers(), "Content-Type": "application/json"}, json=payload, timeout=30)
    if not resp.is_success:
        import logging
        _log = logging.getLogger(__name__)
        try:
            err = resp.json()
            errors = [
                ve.get("Message", "")
                for el in err.get("Elements", [])
                for ve in el.get("ValidationErrors", [])
            ]
            _log.error("Xero POST %s → %s: %s", endpoint, resp.status_code,
                       "; ".join(errors) if errors else resp.text[:500])
        except Exception:
            _log.error("Xero POST %s → %s: %s", endpoint, resp.status_code, resp.text[:500])
    resp.raise_for_status()
    return resp.json()


def paginate(endpoint: str, key: str, params: dict | None = None) -> Generator[Any, None, None]:
    """Yield all items across pages (Xero uses page= param)."""
    page = 1
    while True:
        p = {**(params or {}), "page": page}
        data = get(endpoint, params=p)
        items = data.get(key, [])
        if not items:
            break
        yield from items
        if len(items) < 100:  # Xero default page size.
            break
        page += 1
