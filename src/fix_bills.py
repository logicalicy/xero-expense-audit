"""
Auto-correct engine — declarative strategy chains for fixing flagged bills.
Loads config/xero.config.json and applies strategies in order until one succeeds.
"""

from __future__ import annotations

import base64
import json
import logging
import os
from pathlib import Path
from typing import Any

import anthropic
import yaml

from .xero_client import get, get_bytes, post, put_bytes
from .reference_data_loader import ReferenceData
from .validators.base import Issue

logger = logging.getLogger(__name__)

_config: dict | None = None
_ai_cfg: dict | None = None


def _extract_json(text: str) -> str:
    """Extract the first JSON object or array from a model response.

    Handles:
      - Bare JSON
      - ```json ... ``` fences (with or without language tag)
      - JSON followed by explanation text
    """
    import re
    text = text.strip()
    # Strip opening/closing fences if present.
    fence = re.match(r"^```(?:json)?\s*(.*?)```\s*$", text, re.DOTALL)
    if fence:
        return fence.group(1).strip()
    # Find the first { or [ and extract to its matching closer.
    for start_char, end_char in [('{', '}'), ('[', ']')]:
        idx = text.find(start_char)
        if idx == -1:
            continue
        depth = 0
        for i, ch in enumerate(text[idx:], idx):
            if ch == start_char:
                depth += 1
            elif ch == end_char:
                depth -= 1
                if depth == 0:
                    return text[idx:i + 1]
    return text


def _pick_invoice_attachment(attachments: list[dict]) -> dict | None:
    """Pick the attachment most likely to be the actual invoice/receipt.

    Scoring (higher = better):
      +10  PDF
      +5   Filename contains invoice/receipt/bill/order
      +3   Image (jpg/png/gif/webp) — usable but lower priority than PDF
      +size_bytes / 100_000  (larger = more content, up to ~1 point per 100 KB)
      -100 Our own ecb-fx-* evidence files — always excluded
    """
    INVOICE_KEYWORDS = ("invoice", "receipt", "bill", "order", "statement")

    def _score(att: dict) -> float:
        name = att.get("FileName", "").lower()
        size = att.get("ContentLength", 0) or 0
        if name.startswith("ecb-fx-"):
            return -100
        score = 0.0
        if name.endswith(".pdf"):
            score += 10
        elif any(name.endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".gif", ".webp")):
            score += 3
        if any(kw in name for kw in INVOICE_KEYWORDS):
            score += 5
        score += size / 100_000
        return score

    candidates = [a for a in attachments if _score(a) >= 0]
    if not candidates:
        return None
    return max(candidates, key=_score)


def _load_ai_config() -> dict:
    global _ai_cfg
    if _ai_cfg is None:
        ai_cfg_path = Path(__file__).parent.parent / "config" / "ai.yaml"
        with open(ai_cfg_path) as f:
            _ai_cfg = yaml.safe_load(f)
    return _ai_cfg


def _text_model() -> str:
    return _load_ai_config().get("text_model", "claude-haiku-4-5")


def get_text_model() -> str:
    """Public alias for use outside this module."""
    return _text_model()


def _vision_model() -> str:
    return _load_ai_config().get("vision_model", "claude-haiku-4-5")


def _load_config() -> dict:
    global _config
    if _config is None:
        cfg_path = Path(__file__).parent.parent / "config" / "xero.config.json"
        with open(cfg_path) as f:
            _config = json.load(f)
    return _config


# ---------------------------------------------------------------------------.
# Strategy implementations — each returns a patch dict or {} on failure.
# ---------------------------------------------------------------------------.

def copy_bill_date(bill: dict, ref: ReferenceData) -> dict:
    date = bill.get("Date")
    if date:
        return {"DueDate": date}
    return {}


def supplier_default_account(bill: dict, ref: ReferenceData) -> dict:
    """Fill missing `AccountCode` on each line item from the supplier defaults.

    We look up the bill's `ContactID`, fetch the contact, and use its
    `PurchasesDefaultAccountCode`. Only blank/falsey `AccountCode` values are
    replaced; existing `AccountCode`s are preserved.
    """
    contact_id = bill.get("Contact", {}).get("ContactID")
    if not contact_id:
        return {}
    try:
        data = get(f"Contacts/{contact_id}")
        contacts = data.get("Contacts", [])
        if not contacts:
            return {}
        default_code = contacts[0].get("PurchasesDefaultAccountCode")
        if not default_code:
            return {}
    except Exception:
        logger.exception("Failed to fetch contact %s", contact_id)
        return {}

    patched_lines = []
    for line in bill.get("LineItems", []):
        line_copy = dict(line)
        if not line_copy.get("AccountCode"):
            line_copy["AccountCode"] = default_code
        patched_lines.append(line_copy)
    return {"LineItems": patched_lines}


def infer_from_attachment(bill: dict, ref: ReferenceData) -> dict:
    """Infer missing line-item descriptions by reading the bill attachment.

    - Selects the "best" attachment via `_pick_invoice_attachment` (prefers PDF).
    - Uses the vision model for images; for PDFs it extracts the first few pages
      and uses the text model.
    - Expects the model to return a JSON array of description strings, then
      fills only empty `LineItems[*].Description` fields in order.
    - Returns `{}` if attachments are missing/unsupported or the model output
      cannot be parsed.
    """
    bill_id = bill.get("InvoiceID", "")
    try:
        att_data = get(f"Invoices/{bill_id}/Attachments")
        attachments = att_data.get("Attachments", [])
        if not attachments:
            return {}

        first = _pick_invoice_attachment(attachments)
        if not first:
            return {}
        filename = first.get("FileName", "")
        logger.info("infer_from_attachment: selected '%s' on bill %s", filename, bill_id[:8])
        content_bytes, content_type = get_bytes(
            f"Invoices/{bill_id}/Attachments/{filename}"
        )

        system_prompt = (
            "Extract line item descriptions from this receipt or invoice. "
            "Return a JSON array of description strings — one per line item. "
            "Use the exact wording from the document where possible. "
            "If you cannot identify individual line items, return a single-element array "
            "with a short description of what was purchased (e.g. [\"Monthly software subscription\"]). "
            "Never return an empty array or null. Return ONLY valid JSON."
        )
        client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

        if content_type.startswith("image/"):
            b64 = base64.standard_b64encode(content_bytes).decode("ascii")
            media_type = content_type.split(";")[0].strip()
            msg = client.messages.create(
                model=_vision_model(),
                max_tokens=256,
                system=system_prompt,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}},
                        {"type": "text", "text": "Extract line item descriptions as a JSON array of strings."},
                    ],
                }],
            )
        elif content_type == "application/pdf" or filename.lower().endswith(".pdf"):
            try:
                import pypdf, io as _io
                reader = pypdf.PdfReader(_io.BytesIO(content_bytes))
                text = "\n".join(page.extract_text() or "" for page in reader.pages[:3])
            except Exception:
                logger.warning("infer_from_attachment: pypdf failed for %s", filename)
                return {}
            if not text.strip():
                logger.info("infer_from_attachment: PDF text extraction empty for %s", filename)
                return {}
            msg = client.messages.create(
                model=_text_model(),
                max_tokens=256,
                system=system_prompt,
                messages=[{"role": "user", "content": f"Invoice text:\n{text[:3000]}"}],
            )
        else:
            logger.info("infer_from_attachment: unsupported type %s for %s", content_type, filename)
            return {}
        raw = _extract_json(msg.content[0].text)
        if not raw or raw.lower() == "null":
            logger.info("infer_from_attachment: model returned empty/null for bill %s", bill_id[:8])
            return {}
        descriptions: list[str] = json.loads(raw)

        patched_lines = []
        desc_idx = 0
        # The model returns descriptions "one per line item" in the order it.
        # observes them on the document. We only overwrite when the Xero line.
        # already has a blank description.
        for line in bill.get("LineItems", []):
            line_copy = dict(line)
            if not line_copy.get("Description", "").strip() and desc_idx < len(descriptions):
                line_copy["Description"] = descriptions[desc_idx]
                desc_idx += 1
            patched_lines.append(line_copy)
        return {"LineItems": patched_lines}
    except Exception:
        logger.exception("infer_from_attachment failed for bill %s", bill_id)
        return {}


def account_default_tax_rate(bill: dict, ref: ReferenceData) -> dict:
    """Infer tax type from account code using config/xero.config.json account_tax_defaults.

    Xero contacts do not expose a purchasable default tax type via the API,
    so we use a config-driven account code → TaxType mapping instead.
    Falls back to the _fallback value (default: INPUT2 = 20% VAT on expenses).
    """
    cfg = _load_config().get("account_tax_defaults", {})
    fallback = cfg.get("_fallback", "INPUT2")

    patched_lines = []
    changed = False
    for line in bill.get("LineItems", []):
        line_copy = dict(line)
        if not line_copy.get("TaxType"):
            account_code = str(line_copy.get("AccountCode", ""))
            tax_type = cfg.get(account_code, fallback)
            line_copy["TaxType"] = tax_type
            changed = True
        patched_lines.append(line_copy)
    return {"LineItems": patched_lines} if changed else {}


def infer_supplier_from_attachment(bill: dict, ref: ReferenceData) -> dict:
    """Extract merchant name from receipt via vision or text, fuzzy-match against contacts."""
    bill_id = bill.get("InvoiceID", "")
    try:
        att_data = get(f"Invoices/{bill_id}/Attachments")
        attachments = att_data.get("Attachments", [])
        if not attachments:
            logger.info("infer_supplier_from_attachment: no attachments on bill %s — cannot infer supplier", bill_id[:8])
            return {}

        first = _pick_invoice_attachment(attachments)
        if not first:
            return {}
        filename = first.get("FileName", "")
        logger.info("infer_supplier_from_attachment: selected '%s' on bill %s", filename, bill_id[:8])
        content_bytes, content_type = get_bytes(f"Invoices/{bill_id}/Attachments/{filename}")

        client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

        if content_type.startswith("image/"):
            logger.info("infer_supplier_from_attachment: running vision on %s (%s)", filename, content_type)
            b64 = base64.standard_b64encode(content_bytes).decode("ascii")
            media_type = content_type.split(";")[0].strip()
            msg = client.messages.create(
                model=_vision_model(),
                max_tokens=64,
                system='Extract the merchant/supplier company name from this receipt. Return JSON: {"name": "Company Name", "confidence": 0.9}',
                messages=[{"role": "user", "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}},
                    {"type": "text", "text": "What is the supplier/merchant name on this receipt?"},
                ]}],
            )
        elif content_type == "application/pdf" or filename.lower().endswith(".pdf"):
            # PDF: extract text and ask LLM for supplier name.
            logger.info("infer_supplier_from_attachment: PDF attachment — extracting text from %s", filename)
            try:
                import pypdf
                import io
                reader = pypdf.PdfReader(io.BytesIO(content_bytes))
                text = "\n".join(page.extract_text() or "" for page in reader.pages[:3])
            except Exception:
                logger.warning("infer_supplier_from_attachment: pypdf not available or failed, skipping PDF %s", filename)
                return {}
            if not text.strip():
                logger.info("infer_supplier_from_attachment: PDF text extraction returned empty content")
                return {}
            logger.info("infer_supplier_from_attachment: PDF text preview: %r", text[:300])
            msg = client.messages.create(
                model=_text_model(),
                max_tokens=64,
                system='Extract the merchant/supplier company name from this document. Return JSON: {"name": "Company Name", "confidence": 0.9}',
                messages=[{"role": "user", "content": f"Document text:\n{text[:3000]}"}],
            )
        else:
            logger.info(
                "infer_supplier_from_attachment: unsupported attachment type '%s' (%s) — skipping",
                filename, content_type,
            )
            return {}

        raw = _extract_json(msg.content[0].text)
        if not raw:
            logger.warning("infer_supplier_from_attachment: model returned empty response for bill %s", bill_id[:8])
            return {}
        try:
            result = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("infer_supplier_from_attachment: non-JSON response: %r", raw[:200])
            return {}
        merchant_name = result.get("name", "").lower().strip()
        confidence = float(result.get("confidence", 0))
        logger.info("infer_supplier_from_attachment: AI extracted supplier='%s' confidence=%.2f", merchant_name, confidence)

        if not merchant_name or confidence < 0.5:
            logger.info("infer_supplier_from_attachment: low confidence or empty name — skipping")
            return {}

        # Fuzzy match against known contacts.
        best_match = None
        best_score = 0.0
        for contact_id, contact in ref.suppliers.items():
            contact_name = contact.get("Name", "").lower().strip()
            # Simple word overlap score.
            words_a = set(merchant_name.split())
            words_b = set(contact_name.split())
            if not words_a or not words_b:
                continue
            overlap = len(words_a & words_b) / max(len(words_a), len(words_b))
            if overlap > best_score:
                best_score = overlap
                best_match = contact

        threshold = _load_config().get("auto_correct", {}).get("MISSING_SUPPLIER", {}).get("match_threshold", 0.7)
        if best_match and best_score >= threshold:
            return {"Contact": {"ContactID": best_match["ContactID"], "Name": best_match["Name"]}}

        # Return inferred name so fix-bill can use it interactively even if no match.
        return {"_inferred_supplier_name": merchant_name}

    except Exception:
        logger.exception("infer_supplier_from_attachment failed for bill %s", bill_id)
        return {}


def correct_currency_from_attachment(bill: dict, ref: ReferenceData) -> dict:
    """
    Detect foreign currency in the bill attachment, convert amounts to GBP using the
    ECB reference rate (via frankfurter.app), and record the rate as a Xero history note.

    Steps:
      1. Extract currency + amount from attachment (vision / PDF)
      2. If non-GBP: fetch ECB rate from frankfurter.app + ECB CSV from SDMX API
      3. Convert each line item UnitAmount to GBP
      4. Upload ECB CSV to Xero as audit evidence
      5. Post a Xero history note with rate, date, and link to ECB data browser
      6. Return patch with converted line item amounts + _fx_note for display
    """
    from .currency import extract_currency_and_amount, get_ecb_rate_and_csv, _parse_xero_date, BASE_CURRENCY
    from .xero_client import get_bytes as xero_get_bytes, put_bytes

    bill_id = bill.get("InvoiceID", "")
    try:
        att_data = get(f"Invoices/{bill_id}/Attachments")
        attachments = att_data.get("Attachments", [])
        if not attachments:
            logger.info("correct_currency_from_attachment: no attachments on bill %s", bill_id[:8])
            return {}

        # Pick the best invoice attachment (excludes our own ecb-fx-* evidence files).
        invoice_att = _pick_invoice_attachment(attachments)
        if not invoice_att:
            logger.info("correct_currency_from_attachment: no invoice attachment found on bill %s", bill_id[:8])
            return {}

        filename = invoice_att.get("FileName", "")
        logger.info("correct_currency_from_attachment: checking '%s' on bill %s", filename, bill_id[:8])
        content_bytes, content_type = xero_get_bytes(f"Invoices/{bill_id}/Attachments/{filename}")

        extracted = extract_currency_and_amount(
            content_bytes, content_type, filename,
            text_model=_text_model(),
            vision_model=_vision_model(),
        )
        if not extracted:
            logger.info("correct_currency_from_attachment: could not extract currency from %s", filename)
            return {}

        currency = extracted["currency"]
        invoice_amount = extracted["amount"]
        logger.info("correct_currency_from_attachment: detected %s %.2f on bill %s", currency, invoice_amount, bill_id[:8])

        if currency == BASE_CURRENCY:
            # Currency is already GBP — check if the bill amount is 0 or any line.
            # item has a null UnitAmount. If so, fill in from the attachment.
            bill_total = float(bill.get("Total") or 0)
            line_items = bill.get("LineItems", [])
            amount_missing = bill_total == 0.0 or any(
                line.get("UnitAmount") is None for line in line_items
            )
            if amount_missing and invoice_amount > 0:
                logger.info(
                    "correct_currency_from_attachment: bill is GBP but amount is missing/0 — "
                    "setting UnitAmount from attachment (%.2f)", invoice_amount,
                )
                n_lines = len(line_items) or 1
                per_line = round(invoice_amount / n_lines, 2)
                patched_lines = []
                for line in line_items:
                    line_copy = dict(line)
                    if line_copy.get("UnitAmount") is None or float(line_copy.get("UnitAmount") or 0) == 0.0:
                        line_copy["UnitAmount"] = per_line
                    line_copy.pop("TaxAmount", None)
                    line_copy.pop("LineAmount", None)
                    patched_lines.append(line_copy)
                return {
                    "LineItems": patched_lines,
                    "_fx_note": f"GBP {invoice_amount:.2f} (from attachment — bill had no amount)",
                }
            logger.info("correct_currency_from_attachment: already %s, amount looks correct — no conversion needed", BASE_CURRENCY)
            # Return a sentinel so the UI can distinguish "verified clean" from "couldn't fix".
            return {"_fx_verified": True}

        # Parse bill date for historical rate lookup.
        bill_date = _parse_xero_date(bill.get("Date"))
        if bill_date:
            logger.info("correct_currency_from_attachment: using bill date %s for ECB rate", bill_date)
        else:
            logger.warning("correct_currency_from_attachment: no bill date found, using latest available rate")

        # Fetch ECB rate + CSV evidence for the bill date.
        rate, rate_date, ecb_csv = get_ecb_rate_and_csv(currency, BASE_CURRENCY, bill_date=bill_date)
        logger.info("correct_currency_from_attachment: ECB rate %s→%s = %.6f (%s)", currency, BASE_CURRENCY, rate, rate_date)

        # Check if bill amount already matches the converted amount (within 2%).
        # This happens when the bill was previously fixed — the attachment still says.
        # the foreign amount but the bill has already been converted to GBP.
        expected_gbp = round(invoice_amount * rate, 2)
        bill_total = float(bill.get("Total") or 0)
        if bill_total > 0 and abs(bill_total - expected_gbp) / expected_gbp < 0.02:
            logger.info(
                "correct_currency_from_attachment: bill total %.2f ≈ expected converted %.2f — already converted",
                bill_total, expected_gbp,
            )
            # Amount is correct but tax type may still be wrong — foreign suppliers.
            # must be NONE regardless. Fix silently without a full FX conversion.
            wrong_tax_lines = [
                line for line in bill.get("LineItems", [])
                if line.get("TaxType") and line.get("TaxType") != "NONE"
            ]
            if wrong_tax_lines:
                logger.info(
                    "correct_currency_from_attachment: fixing TaxType → NONE on %d line(s) for foreign bill",
                    len(wrong_tax_lines),
                )
                patched = []
                for line in bill.get("LineItems", []):
                    lc = dict(line)
                    lc["TaxType"] = "NONE"
                    lc.pop("TaxAmount", None)
                    lc.pop("LineAmount", None)
                    patched.append(lc)
                return {"LineItems": patched, "_fx_verified": True}
            return {"_fx_verified": True}

        # Convert line item amounts.
        patched_lines = []
        for line in bill.get("LineItems", []):
            line_copy = dict(line)
            orig = float(line_copy.get("UnitAmount") or 0)
            if orig:
                line_copy["UnitAmount"] = round(orig * rate, 2)
            # Foreign currency = non-UK supplier = outside scope of UK VAT → always NONE.
            line_copy["TaxType"] = "NONE"
            # Drop calculated fields — let Xero recompute from the new UnitAmount.
            line_copy.pop("TaxAmount", None)
            line_copy.pop("LineAmount", None)
            patched_lines.append(line_copy)

        ecb_filename = f"ecb-fx-{currency}-{BASE_CURRENCY}-{rate_date}.csv"
        date_note = rate_date
        if bill_date and rate_date != bill_date:
            date_note += f" (invoice date {bill_date} fell on a non-business day)"

        # Build ECB SDMX API links with date range so they return actual rate data.
        # For cross-rates (neither currency is EUR), we use two ECB series.
        #   {currency}/EUR and GBP/EUR → cross-rate = GBP/EUR ÷ {currency}/EUR.
        # Link to the API endpoint (data-api.ecb.europa.eu) not the portal UI which.
        # hides values behind a login wall.
        def _ecb_api_link(series_currency: str) -> str:
            return (
                f"https://data-api.ecb.europa.eu/service/data/EXR/"
                f"D.{series_currency}.EUR.SP00.A"
                f"?format=csvdata&startPeriod={rate_date}&endPeriod={rate_date}"
            )

        if currency == "EUR":
            # EUR → GBP: only need GBP/EUR series.
            history_note = (
                f"FX: 1 {currency} = {rate:.6f} {BASE_CURRENCY} · ECB rate {date_note} · "
                f"{_ecb_api_link(BASE_CURRENCY)}"
            )
        elif currency == BASE_CURRENCY:
            # Already GBP — should not reach here but guard anyway.
            history_note = f"FX: no conversion needed (already {BASE_CURRENCY})"
        else:
            # Cross-rate via EUR: e.g. USD→GBP uses USD/EUR and GBP/EUR series.
            history_note = (
                f"FX: 1 {currency} = {rate:.6f} {BASE_CURRENCY} · "
                f"ECB cross-rate {date_note} ({currency}/EUR ÷ {BASE_CURRENCY}/EUR) · "
                f"{currency}/EUR: {_ecb_api_link(currency)} · "
                f"{BASE_CURRENCY}/EUR: {_ecb_api_link(BASE_CURRENCY)}"
            )

        gbp_total = round(invoice_amount * rate, 2)
        fx_note = f"{currency} {invoice_amount:.2f} → {BASE_CURRENCY} {gbp_total:.2f} @ {rate:.6f} (ECB ref rate {rate_date}"
        if bill_date and rate_date != bill_date:
            fx_note += f", nearest business day to invoice date {bill_date}"
        fx_note += ")"

        # Stash side-effect data — executed by push_fix AFTER successful write.
        return {
            "LineItems": patched_lines,
            "_fx_note": fx_note,
            "_post_write_csv": (ecb_filename, ecb_csv),
            "_post_write_note": history_note,
        }

    except Exception:
        logger.exception("correct_currency_from_attachment failed for bill %s", bill_id)
        return {}


def ai_infer_from_context(bill: dict, ref: ReferenceData) -> dict:
    """Suggest a short expense description from bill metadata.

    Uses Anthropic to produce plain-text text (prompted as <= 10 words).
    Only fills `LineItems[*].Description` fields that are currently blank.
    """
    try:
        supplier = bill.get("Contact", {}).get("Name", "Unknown")
        total = bill.get("Total", "")
        currency = bill.get("CurrencyCode", "")
        date = bill.get("Date", "")

        client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
        msg = client.messages.create(
            model=_text_model(),
            max_tokens=64,
            system="Suggest a short expense description (max 10 words). Return plain text only.",
            messages=[{
                "role": "user",
                "content": f"Supplier: {supplier}, Total: {currency} {total}, Date: {date}",
            }],
        )
        description = msg.content[0].text.strip()
        if not description:
            return {}

        patched_lines = []
        for line in bill.get("LineItems", []):
            line_copy = dict(line)
            if not line_copy.get("Description", "").strip():
                line_copy["Description"] = description
            patched_lines.append(line_copy)
        return {"LineItems": patched_lines}
    except Exception:
        logger.exception("ai_infer_from_context failed")
        return {}


# ---------------------------------------------------------------------------.
# Strategy registry.
# ---------------------------------------------------------------------------.

_STRATEGIES: dict[str, Any] = {
    "copy_bill_date": copy_bill_date,
    "supplier_default_account": supplier_default_account,
    "account_default_tax_rate": account_default_tax_rate,
    "infer_supplier_from_attachment": infer_supplier_from_attachment,
    "infer_from_attachment": infer_from_attachment,
    "correct_currency_from_attachment": correct_currency_from_attachment,
    "ai_infer_from_context": ai_infer_from_context,
}


# ---------------------------------------------------------------------------.
# Deep merge line items by index.
# ---------------------------------------------------------------------------.

def _deep_merge(base: dict, patch: dict) -> dict:
    """Merge patch into base. For LineItems, merge by index."""
    merged = dict(base)
    for key, val in patch.items():
        if key == "LineItems" and "LineItems" in merged:
            base_lines = merged["LineItems"]
            patch_lines = val
            result_lines = []
            for i in range(max(len(base_lines), len(patch_lines))):
                if i < len(base_lines) and i < len(patch_lines):
                    result_lines.append({**base_lines[i], **patch_lines[i]})
                elif i < len(patch_lines):
                    result_lines.append(patch_lines[i])
                else:
                    result_lines.append(base_lines[i])
            merged["LineItems"] = result_lines
        else:
            merged[key] = val
    return merged


# ---------------------------------------------------------------------------.
# Public API.
# ---------------------------------------------------------------------------.

def apply_fixes(bill: dict, issues: list[Issue], ref: ReferenceData, config: dict) -> dict:
    """Try strategy chains for each issue. Returns merged patch dict."""
    auto_correct = config.get("auto_correct", {})
    merged_patch: dict = {}

    for issue in issues:
        rule = auto_correct.get(issue.code)
        if not rule or not rule.get("enabled"):
            continue

        for strategy_name in rule.get("strategies", []):
            fn = _STRATEGIES.get(strategy_name)
            if not fn:
                logger.warning("Unknown strategy: %s", strategy_name)
                continue
            patch = fn(bill, ref)
            if patch:
                merged_patch = _deep_merge(merged_patch, patch)
                break  # stop on first success for this issue.

    return merged_patch


def push_fix(bill_id: str, patch: dict, bill: dict, _strip_keys: tuple = ("_inferred_supplier_name", "_fx_note", "_fx_verified", "_post_write_csv", "_post_write_note")) -> bool:
    """POST patch to Xero. Returns True on success, False on error.

    Xero POST /Invoices/{InvoiceID} requires Type, Contact, and LineItems
    (with LineItemIDs preserved to avoid deletion). We build the full required
    payload from the original bill merged with the patch.
    """
    try:
        # Extract post-write side effects before stripping.
        post_write_csv: tuple | None = patch.get("_post_write_csv")
        post_write_note: str | None = patch.get("_post_write_note")

        # Strip internal metadata keys before sending to Xero.
        patch = {k: v for k, v in patch.items() if k not in _strip_keys}

        # Preserve LineItemIDs so Xero updates in place rather than delete+create.
        # Strip computed fields that Xero recalculates — sending stale values causes 400.
        _LINE_COMPUTED = {"LineAmount", "TaxAmount", "Validation", "ValidationErrors"}
        patch_lines = patch.get("LineItems", [])
        orig_lines = bill.get("LineItems", [])
        merged_lines = []
        for i, orig in enumerate(orig_lines):
            line = dict(orig)
            if i < len(patch_lines):
                line.update(patch_lines[i])
            for f in _LINE_COMPUTED:
                line.pop(f, None)
            merged_lines.append(line)

        # Fields Xero recomputes from line items — sending stale values causes 400.
        _XERO_COMPUTED = {
            "SubTotal", "TotalTax", "Total", "AmountDue", "AmountPaid",
            "AmountCredited", "FullyPaidOnDate", "UpdatedDateUTC",
        }

        payload = {
            "InvoiceID": bill_id,
            "Type": bill.get("Type", "ACCPAY"),
            "Contact": patch.get("Contact") or bill.get("Contact", {}),
            "LineItems": merged_lines,
        }
        # Merge remaining patch fields (DueDate, Reference, etc.) — skip computed fields.
        for k, v in patch.items():
            if k not in ("Contact", "LineItems") and k not in _XERO_COMPUTED:
                payload[k] = v

        post(f"Invoices/{bill_id}", payload)

        # Side effects — only run after successful write.
        if post_write_csv:
            ecb_filename, ecb_csv = post_write_csv
            try:
                put_bytes(
                    f"Invoices/{bill_id}/Attachments/{ecb_filename}",
                    ecb_csv, "text/csv", ecb_filename,
                )
                logger.info("push_fix: uploaded ECB CSV as %s", ecb_filename)
            except Exception:
                logger.warning("push_fix: failed to upload ECB CSV — continuing")

        if post_write_note:
            try:
                post(
                    f"Invoices/{bill_id}/History",
                    {"HistoryRecords": [{"Details": post_write_note}]},
                )
                logger.info("push_fix: posted history note to bill %s", bill_id[:8])
            except Exception:
                logger.warning("push_fix: failed to post history note — continuing")

        return True
    except Exception:
        logger.exception("push_fix failed for bill %s", bill_id)
        return False
