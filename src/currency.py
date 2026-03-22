"""
Currency detection and FX conversion for bill attachments.

Rate source : European Central Bank (ECB) SDMX REST API
              https://data-api.ecb.europa.eu — official, open, no auth, real CSV output
Evidence    : Raw ECB CSV uploaded to Xero as attachment
Calculation : For non-EUR pairs, cross-rate via EUR is computed from two ECB series.

Note: Xero multicurrency (Settings → Currencies) would handle this natively, but this
module exists for orgs on plans that don't include multicurrency.
"""

from __future__ import annotations

import base64
import csv
import io
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

import httpx

logger = logging.getLogger(__name__)

BASE_CURRENCY = os.environ.get("BASE_CURRENCY", "GBP")
ECB_API = "https://data-api.ecb.europa.eu/service/data/EXR"


# ---------------------------------------------------------------------------.
# ECB rate fetch — returns rate + raw CSV bytes as evidence.
# ---------------------------------------------------------------------------.

def _parse_xero_date(xero_date: str | None) -> str | None:
    """
    Parse Xero's /Date(1234567890000+0000)/ format to ISO date string (YYYY-MM-DD).
    Returns None if the value is missing or unparseable.
    """
    import re
    if not xero_date:
        return None
    m = re.match(r"/Date\((\d+)([+-]\d{4})?\)/", xero_date)
    if not m:
        return None
    ts = int(m.group(1)) / 1000
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def _fetch_ecb_series(currency: str, date: str | None = None) -> tuple[float, bytes]:
    """
    Fetch the ECB reference rate for currency/EUR on a specific date (YYYY-MM-DD).
    If date is None or no data exists for that date (weekend/holiday), walks back
    up to 7 days to find the nearest published rate.
    Returns (rate_vs_eur, csv_bytes).
    """
    from datetime import date as date_cls, timedelta
    import re

    target = datetime.strptime(date, "%Y-%m-%d").date() if date else datetime.now(timezone.utc).date()

    for days_back in range(8):  # try target date, then up to 7 days back.
        candidate = target - timedelta(days=days_back)
        candidate_str = candidate.isoformat()
        url = (
            f"{ECB_API}/D.{currency.upper()}.EUR.SP00.A"
            f"?format=csvdata&startPeriod={candidate_str}&endPeriod={candidate_str}"
        )
        resp = httpx.get(url, timeout=15, follow_redirects=True)
        resp.raise_for_status()
        csv_bytes = resp.content

        reader = csv.DictReader(io.StringIO(csv_bytes.decode("utf-8")))
        rows = [r for r in reader if r.get("OBS_VALUE", "").strip()]
        if rows:
            rate = float(rows[-1]["OBS_VALUE"])
            return rate, csv_bytes

    raise ValueError(
        f"ECB returned no data for {currency}/EUR near {target} (checked 7 days back). "
        "Currency may not be covered by ECB reference rates."
    )


def get_ecb_rate_and_csv(
    from_currency: str,
    to_currency: str = BASE_CURRENCY,
    bill_date: str | None = None,
) -> tuple[float, str, bytes]:
    """
    Fetch ECB cross-rate from_currency → to_currency (via EUR).
    Returns (rate, rate_date, combined_csv_bytes).

    Rate calculation:
        from_per_eur = ECB series D.{from}.EUR  (e.g. 1 EUR = 1.1555 USD)
        to_per_eur   = ECB series D.{to}.EUR    (e.g. 1 EUR = 0.8644 GBP)
        rate         = to_per_eur / from_per_eur (e.g. 1 USD = 0.7481 GBP)
    """
    from_currency = from_currency.upper()
    to_currency = to_currency.upper()

    if from_currency == to_currency:
        dummy = f"DATE,FROM,TO,RATE\n{datetime.now(timezone.utc).date().isoformat()},{from_currency},{to_currency},1.0\n"
        return 1.0, datetime.now(timezone.utc).date().isoformat(), dummy.encode()

    if from_currency == "EUR":
        to_per_eur, to_csv = _fetch_ecb_series(to_currency, date=bill_date)
        rate = to_per_eur
        combined_csv = to_csv
        rate_date = _latest_date_from_csv(to_csv)
    elif to_currency == "EUR":
        from_per_eur, from_csv = _fetch_ecb_series(from_currency, date=bill_date)
        rate = 1.0 / from_per_eur
        combined_csv = from_csv
        rate_date = _latest_date_from_csv(from_csv)
    else:
        # Cross via EUR: rate = to_per_eur / from_per_eur.
        from_per_eur, from_csv = _fetch_ecb_series(from_currency, date=bill_date)
        to_per_eur, to_csv = _fetch_ecb_series(to_currency, date=bill_date)
        rate = to_per_eur / from_per_eur
        rate_date = _latest_date_from_csv(from_csv)
        derived = (
            f"\n# DERIVED CROSS RATE\n"
            f"# 1 {from_currency} = {rate:.6f} {to_currency}\n"
            f"# Source: ECB reference rates ({rate_date})\n"
            f"# Formula: {to_currency}/EUR ({to_per_eur:.6f}) / {from_currency}/EUR ({from_per_eur:.6f})\n\n"
            f"ECB Series,{from_currency}/EUR\n"
        )
        combined_csv = (
            derived.encode()
            + from_csv
            + b"\n\nECB Series," + to_currency.encode() + b"/EUR\n"
            + to_csv
        )

    return rate, rate_date, combined_csv


def _latest_date_from_csv(csv_bytes: bytes) -> str:
    reader = csv.DictReader(io.StringIO(csv_bytes.decode("utf-8")))
    rows = list(reader)
    return rows[-1]["TIME_PERIOD"] if rows else datetime.now(timezone.utc).date().isoformat()


# ---------------------------------------------------------------------------.
# Currency extraction from attachment.
# ---------------------------------------------------------------------------.

def extract_currency_and_amount(
    content_bytes: bytes,
    content_type: str,
    filename: str,
    text_model: str,
    vision_model: str,
) -> dict[str, Any] | None:
    """
    Extract currency code and total amount from a bill attachment (image or PDF).
    Returns {"currency": "USD", "amount": 99.99} or None on failure.
    """
    import anthropic

    client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
    system = (
        'Extract the total amount and currency from this invoice/receipt. '
        'Return ONLY JSON: {"currency": "USD", "amount": 99.99}. '
        'Use ISO 4217 currency codes (GBP, USD, EUR, JPY, etc.). '
        'Infer from symbols if needed: £=GBP, $=USD, €=EUR, ¥=JPY. '
        'Return null if you cannot determine both fields with confidence.'
    )

    if content_type.startswith("image/"):
        b64 = base64.standard_b64encode(content_bytes).decode("ascii")
        media_type = content_type.split(";")[0].strip()
        msg = client.messages.create(
            model=vision_model,
            max_tokens=64,
            system=system,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}},
                    {"type": "text", "text": "What is the total amount and currency on this invoice?"},
                ],
            }],
        )
    elif content_type == "application/pdf" or filename.lower().endswith(".pdf"):
        try:
            import pypdf
            reader = pypdf.PdfReader(io.BytesIO(content_bytes))
            text = "\n".join(page.extract_text() or "" for page in reader.pages[:3])
        except Exception:
            logger.warning("pypdf extraction failed for %s", filename)
            return None
        if not text.strip():
            return None
        msg = client.messages.create(
            model=text_model,
            max_tokens=64,
            system=system,
            messages=[{"role": "user", "content": f"Invoice text:\n{text[:3000]}"}],
        )
    else:
        logger.info("Unsupported attachment type: %s (%s)", filename, content_type)
        return None

    from .fix_bills import _extract_json
    raw = _extract_json(msg.content[0].text)
    if not raw or raw.lower() == "null":
        return None
    try:
        result = json.loads(raw)
        currency = (result.get("currency") or "").upper().strip()
        amount_raw = result.get("amount")
        if not currency or amount_raw is None:
            # Model returned null — attachment doesn't contain extractable amount.
            logger.info("extract_currency_and_amount: model returned null currency/amount — attachment may not be a machine-readable receipt")
            return None
        amount = float(amount_raw)
        if amount <= 0:
            return None
        return {"currency": currency, "amount": amount}
    except Exception:
        logger.info("extract_currency_and_amount: could not parse model response: %r", raw[:200])
        return None
