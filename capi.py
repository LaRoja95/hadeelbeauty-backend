"""TikTok Events API (server-side Conversions API) for HadeelBeauty.

Event names used here are TikTok's OWN standard event names throughout
(PageView, ViewContent, AddToCart, InitiateCheckout, CompletePayment) so
there is no internal-name-to-platform-name mapping step that can drift out
of sync.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from typing import Any

import httpx

logger = logging.getLogger("hadeelbeauty.capi")

TIKTOK_EVENTS_URL = "https://business-api.tiktok.com/open_api/v1.3/event/track/"

STANDARD_EVENTS = frozenset(
    {"PageView", "ViewContent", "AddToCart", "InitiateCheckout", "CompletePayment"}
)


def capi_logging_enabled() -> bool:
    return os.getenv("TRACKING_LOG_CAPI", "true").lower() == "true"


def log_capi(message: str, **fields: Any) -> None:
    if not capi_logging_enabled():
        return
    extras = " ".join(f"{key}={value}" for key, value in fields.items())
    logger.info("%s %s", message, extras)


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def digits_only(value: str) -> str:
    return re.sub(r"\D", "", value or "")


def normalize_phone_e164(phone_raw: str) -> str:
    """Best-effort Algerian phone normalization to +213XXXXXXXXX."""
    digits = digits_only(phone_raw)
    if not digits:
        return ""
    if digits.startswith("213"):
        return f"+{digits}"
    if digits.startswith("0"):
        return f"+213{digits[1:]}"
    if len(digits) == 9 and digits[0] in "567":
        return f"+213{digits}"
    return f"+{digits}"


def tiktok_hash_phone(phone_raw: str) -> str:
    e164 = normalize_phone_e164(phone_raw)
    return sha256_hex(e164) if e164 else ""


def tiktok_hash_email(email: str) -> str:
    return sha256_hex(email.strip().lower()) if email else ""


def tiktok_config() -> tuple[str, str]:
    pixel_id = os.getenv("TIKTOK_PIXEL_ID", "").strip()
    token = os.getenv("TIKTOK_ACCESS_TOKEN", "").strip()
    return pixel_id, token


async def send_tiktok_event(
    *,
    event_name: str,
    event_id: str,
    payload: dict[str, Any],
    ip: str,
    user_agent: str,
) -> dict[str, Any]:
    pixel_id, token = tiktok_config()
    if not pixel_id or not token:
        log_capi("capi_skip", reason="not_configured", event=event_name, event_id=event_id)
        return {"ok": False, "skipped": True, "reason": "not_configured"}

    user: dict[str, Any] = {}
    phone_hash = tiktok_hash_phone(payload.get("phone", ""))
    email_hash = tiktok_hash_email(payload.get("email", ""))
    if phone_hash:
        user["phone"] = phone_hash
    if email_hash:
        user["email"] = email_hash
    if payload.get("ttclid"):
        user["ttclid"] = payload["ttclid"]
    if ip:
        user["ip"] = ip
    if user_agent:
        user["user_agent"] = user_agent

    properties: dict[str, Any] = {}
    if payload.get("currency"):
        properties["currency"] = payload["currency"]
    if payload.get("value") is not None:
        properties["value"] = float(payload["value"])
    if payload.get("productIds"):
        properties["content_ids"] = payload["productIds"]
        properties["contents"] = [
            {"content_id": pid, "quantity": 1} for pid in payload["productIds"]
        ]

    page: dict[str, Any] = {}
    if payload.get("pageUrl"):
        page["url"] = payload["pageUrl"]

    event_data: dict[str, Any] = {
        "event": event_name,
        "event_time": int(time.time()),
        "event_id": event_id,
    }
    if user:
        event_data["user"] = user
    if properties:
        event_data["properties"] = properties
    if page:
        event_data["page"] = page

    body = {
        "event_source": "web",
        "event_source_id": pixel_id,
        "data": [event_data],
    }
    headers = {"Access-Token": token, "Content-Type": "application/json"}
    log_capi("capi_send", event=event_name, event_id=event_id, pixel_id=pixel_id)

    async with httpx.AsyncClient(timeout=12.0) as client:
        response = await client.post(TIKTOK_EVENTS_URL, headers=headers, json=body)

    ok = response.is_success
    summary = response.text[:500]
    try:
        data = response.json()
        summary = json.dumps(data, ensure_ascii=False)[:500]
        ok = response.is_success and data.get("code", -1) == 0
    except json.JSONDecodeError:
        pass

    if not ok:
        logger.warning("capi_error event_id=%s status=%s body=%s", event_id, response.status_code, summary)

    log_capi("capi_result", event=event_name, event_id=event_id, status=response.status_code, ok=ok)
    return {"ok": ok, "skipped": False, "status": response.status_code, "summary": summary}


async def dispatch_capi_event(
    *, event_name: str, event_id: str, payload: dict[str, Any], ip: str, user_agent: str
) -> dict[str, Any]:
    if event_name not in STANDARD_EVENTS:
        log_capi("capi_skip", reason="unknown_event", event=event_name, event_id=event_id)
        return {"ok": False, "skipped": True, "reason": "unknown_event"}

    results: dict[str, Any] = {}

    # TikTok CAPI
    results["tiktok"] = await send_tiktok_event(
        event_name=event_name, event_id=event_id, payload=payload, ip=ip, user_agent=user_agent
    )

    # Meta CAPI
    results["meta"] = await send_meta_event(
        event_name=event_name, event_id=event_id, payload=payload, ip=ip, user_agent=user_agent
    )

    return results


# ── Meta Conversions API ──────────────────────────────────────────────────────

META_CAPI_URL = "https://graph.facebook.com/v19.0/{pixel_id}/events"

# Map internal TikTok-style event names → Meta standard event names
_META_EVENT_MAP: dict[str, str] = {
    "PageView": "PageView",
    "ViewContent": "ViewContent",
    "AddToCart": "AddToCart",
    "InitiateCheckout": "InitiateCheckout",
    "CompletePayment": "Purchase",
}


def meta_config() -> tuple[str, str]:
    pixel_id = os.getenv("META_PIXEL_ID", "").strip()
    token = os.getenv("META_ACCESS_TOKEN", "").strip()
    return pixel_id, token


async def send_meta_event(
    *,
    event_name: str,
    event_id: str,
    payload: dict[str, Any],
    ip: str,
    user_agent: str,
) -> dict[str, Any]:
    pixel_id, token = meta_config()
    if not pixel_id or not token:
        log_capi("meta_skip", reason="not_configured", event=event_name, event_id=event_id)
        return {"ok": False, "skipped": True, "reason": "not_configured"}

    meta_event_name = _META_EVENT_MAP.get(event_name, event_name)

    # User data — all fields hashed with SHA-256 as required by Meta
    user_data: dict[str, Any] = {}
    phone_raw = payload.get("phone", "")
    if phone_raw:
        digits = digits_only(phone_raw)
        if digits.startswith("213"):
            e164 = f"+{digits}"
        elif digits.startswith("0"):
            e164 = f"+213{digits[1:]}"
        else:
            e164 = f"+213{digits}"
        user_data["ph"] = sha256_hex(e164)
    if ip:
        user_data["client_ip_address"] = ip
    if user_agent:
        user_data["client_user_agent"] = user_agent
    if payload.get("fbp"):
        user_data["fbp"] = payload["fbp"]
    if payload.get("fbc"):
        user_data["fbc"] = payload["fbc"]

    # Custom data
    custom_data: dict[str, Any] = {}
    if payload.get("currency"):
        custom_data["currency"] = payload["currency"]
    if payload.get("value") is not None:
        custom_data["value"] = float(payload["value"])
    if payload.get("productIds"):
        custom_data["content_ids"] = payload["productIds"]
        custom_data["content_type"] = "product"
        custom_data["contents"] = [
            {"id": pid, "quantity": 1} for pid in payload["productIds"]
        ]
    if meta_event_name == "Purchase" and payload.get("value"):
        custom_data["order_id"] = event_id

    event: dict[str, Any] = {
        "event_name": meta_event_name,
        "event_time": int(time.time()),
        "event_id": event_id,
        "action_source": "website",
        "user_data": user_data,
    }
    if custom_data:
        event["custom_data"] = custom_data
    if payload.get("pageUrl"):
        event["event_source_url"] = payload["pageUrl"]

    url = META_CAPI_URL.format(pixel_id=pixel_id)
    body = {"data": [event]}

    log_capi("meta_send", event=meta_event_name, event_id=event_id, pixel_id=pixel_id)

    async with httpx.AsyncClient(timeout=12.0) as client:
        response = await client.post(url, params={"access_token": token}, json=body)

    ok = response.is_success
    summary = response.text[:500]
    try:
        data = response.json()
        summary = json.dumps(data, ensure_ascii=False)[:500]
        ok = response.is_success and data.get("events_received", 0) > 0
    except json.JSONDecodeError:
        pass

    if not ok:
        logger.warning("meta_error event_id=%s status=%s body=%s", event_id, response.status_code, summary)

    log_capi("meta_result", event=meta_event_name, event_id=event_id, status=response.status_code, ok=ok)
    return {"ok": ok, "skipped": False, "status": response.status_code, "summary": summary}
