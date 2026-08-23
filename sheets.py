"""Sync confirmed orders to Google Sheets (Service Account API or Apps Script webhook)."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import httpx

logger = logging.getLogger("hadeelbeauty.sheets")

SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"
DEFAULT_SHEET_TAB = "Feuille 1"
_resolved_tab: str | None = None

# Must match row 1 in "Order HadeelBeauty" Google Sheet.
HEADERS = [
    "date",
    "order id",
    "wilaya",
    "baladia",
    "name",
    "phone",
    "product",
    "sku",
    "quantity",
    "total price",
    "delivery location",
]


def sheets_configured() -> bool:
    if _service_account_info() is not None and os.getenv("GOOGLE_SHEETS_ID", "").strip():
        return True
    return bool(os.getenv("GOOGLE_SHEETS_WEBHOOK_URL", "").strip())


def _parse_items(items: list[dict[str, Any]] | str) -> list[dict[str, Any]]:
    if isinstance(items, str):
        return json.loads(items)
    return items or []


def _format_created(created: Any) -> str:
    if isinstance(created, datetime):
        return created.astimezone(timezone.utc).strftime("%m/%d/%Y %H:%M")
    return str(created or "")


def build_order_row(order: dict[str, Any], region_name: str, status: str = "Confirmed") -> list[Any]:
    items = _parse_items(order.get("items") or [])
    product_names: list[str] = []
    skus: list[str] = []
    quantities: list[str] = []

    for item in items:
        product_names.append(str(item.get("name") or item.get("product_id") or "منتج"))
        skus.append(str(item.get("product_id") or item.get("sku") or ""))
        quantities.append(str(item.get("quantity", 1)))

    total = int(order.get("total_sar") or 0)

    return [
        _format_created(order.get("created_at")),
        order.get("id", ""),
        region_name,
        order.get("city", ""),
        order.get("name", ""),
        order.get("phone_raw", ""),
        " / ".join(product_names),
        " / ".join([s for s in skus if s]),
        " / ".join(quantities) if quantities else "1",
        total,
        order.get("address", ""),
    ]


def build_order_payload(order: dict[str, Any], region_name: str, status: str = "Confirmed") -> dict[str, Any]:
    row = build_order_row(order, region_name, status=status)
    flat: dict[str, Any] = {"action": "append", "orderId": order.get("id", "")}
    for header, value in zip(HEADERS, row, strict=True):
        flat[header] = value

    secret = os.getenv("GOOGLE_SHEETS_WEBHOOK_SECRET", "").strip()
    if secret:
        flat["secret"] = secret

    return {"row": row, "flat": flat, "orderId": order.get("id", "")}


def _service_account_info() -> dict[str, Any] | None:
    b64 = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON_B64", "").strip()
    if b64:
        try:
            raw = base64.b64decode(b64).decode("utf-8")
            return json.loads(raw)
        except (ValueError, json.JSONDecodeError):
            logger.error("GOOGLE_SERVICE_ACCOUNT_JSON_B64 is invalid")
            return None

    raw = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        logger.error("GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON")
        return None


def _fetch_access_token(creds_info: dict[str, Any]) -> str:
    from google.auth.transport.requests import Request
    from google.oauth2 import service_account

    creds = service_account.Credentials.from_service_account_info(creds_info, scopes=[SHEETS_SCOPE])
    creds.refresh(Request())
    if not creds.token:
        raise RuntimeError("Failed to obtain Google access token")
    return creds.token


async def _resolve_sheet_tab(token: str, sheet_id: str) -> str:
    global _resolved_tab
    custom = os.getenv("GOOGLE_SHEETS_TAB", "").strip()
    if custom:
        return custom
    if _resolved_tab:
        return _resolved_tab

    url = f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}?fields=sheets.properties.title"
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.get(url, headers={"Authorization": f"Bearer {token}"})
        response.raise_for_status()
        data = response.json()

    sheets = data.get("sheets") or []
    if not sheets:
        _resolved_tab = DEFAULT_SHEET_TAB
        return _resolved_tab
    _resolved_tab = sheets[0].get("properties", {}).get("title") or DEFAULT_SHEET_TAB
    return _resolved_tab


async def _sheets_get(token: str, sheet_id: str, range_a1: str) -> dict[str, Any]:
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values/{quote(range_a1, safe='')}"
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.get(url, headers={"Authorization": f"Bearer {token}"})
        response.raise_for_status()
        return response.json()


async def _sheets_put(token: str, sheet_id: str, range_a1: str, values: list[list[Any]]) -> None:
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values/{quote(range_a1, safe='')}"
    params = {"valueInputOption": "USER_ENTERED"}
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.put(
            url,
            params=params,
            json={"values": values},
            headers={"Authorization": f"Bearer {token}"},
        )
        response.raise_for_status()


async def _ensure_headers(token: str, sheet_id: str) -> None:
    tab = await _resolve_sheet_tab(token, sheet_id)
    header_range = f"'{tab}'!A1:K1"
    data = await _sheets_get(token, sheet_id, header_range)
    values = data.get("values") or []
    if values and any(str(cell).strip() for cell in values[0]):
        return
    await _sheets_put(token, sheet_id, header_range, [HEADERS])


async def _append_via_api(payload: dict[str, Any]) -> None:
    sheet_id = os.getenv("GOOGLE_SHEETS_ID", "").strip()
    creds_info = _service_account_info()
    if not sheet_id or not creds_info:
        return

    token = await asyncio.to_thread(_fetch_access_token, creds_info)
    tab = await _resolve_sheet_tab(token, sheet_id)
    await _ensure_headers(token, sheet_id)

    append_range = f"'{tab}'!A:K"
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values/{quote(append_range, safe='')}:append"
    params = {"valueInputOption": "USER_ENTERED", "insertDataOption": "INSERT_ROWS"}
    body = {"values": [payload["row"]]}

    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.post(
            url,
            params=params,
            json=body,
            headers={"Authorization": f"Bearer {token}"},
        )
        response.raise_for_status()


async def _append_via_webhook(payload: dict[str, Any]) -> None:
    webhook_url = os.getenv("GOOGLE_SHEETS_WEBHOOK_URL", "").strip()
    if not webhook_url:
        return

    body = payload["flat"]
    body_bytes = json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "text/plain; charset=utf-8"}

    async with httpx.AsyncClient(timeout=20.0, follow_redirects=False) as client:
        response = await client.post(webhook_url, content=body_bytes, headers=headers)
        if response.status_code in (301, 302, 303, 307, 308):
            location = response.headers.get("Location")
            if location:
                response = await client.get(location)
        response.raise_for_status()


async def append_order_to_sheet(order: dict[str, Any], region_name: str, status: str = "Confirmed") -> None:
    payload = build_order_payload(order, region_name, status=status)

    try:
        if os.getenv("GOOGLE_SHEETS_WEBHOOK_URL", "").strip():
            await _append_via_webhook(payload)
            logger.info("Order %s synced to Google Sheets (webhook)", payload["orderId"])
            return

        if _service_account_info() is not None and os.getenv("GOOGLE_SHEETS_ID", "").strip():
            await _append_via_api(payload)
            logger.info("Order %s synced to Google Sheets (API)", payload["orderId"])
            return

        logger.warning("Google Sheets sync skipped — not configured")
    except Exception:
        logger.exception("Failed to sync order %s to Google Sheets", payload.get("orderId"))


def schedule_order_sheet_sync(order: dict[str, Any], region_name: str, status: str = "Confirmed") -> None:
    if not sheets_configured():
        return
    asyncio.create_task(append_order_to_sheet(order, region_name, status=status))
