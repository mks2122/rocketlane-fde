"""Provision Vapi: import the Twilio number and create or update the saved assistant.

Idempotent: re-running updates the existing assistant (matched by id, then by name) and
reuses an already-imported number. Prints the ids to put in .env.

Needs in .env: VAPI_API_KEY, PUBLIC_BASE_URL (ngrok), VAPI_WEBHOOK_SECRET, and either
VAPI_PHONE_NUMBER_ID or TWILIO_ACCOUNT_SID + TWILIO_AUTH_TOKEN + TWILIO_PHONE_NUMBER.

    uv run python scripts/setup_vapi_assistant.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Settings, get_settings
from app.errors import AppError
from app.integrations.http import HttpService
from app.integrations.vapi_setup import assistant_payload, twilio_import_payload, webhook_url


async def ensure_phone_number(vapi: HttpService, settings: Settings) -> str:
    if settings.vapi_phone_number_id:
        await vapi.request("GET", f"/phone-number/{settings.vapi_phone_number_id}")
        return settings.vapi_phone_number_id
    settings.require("twilio_account_sid", "twilio_auth_token", "twilio_phone_number")
    assert settings.twilio_auth_token is not None  # noqa: S101  # checked by require()

    numbers: list[dict[str, Any]] = (await vapi.request("GET", "/phone-number")).json()
    existing = next((n for n in numbers if n.get("number") == settings.twilio_phone_number), None)
    if existing is not None:
        print(f"Reusing imported number {settings.twilio_phone_number}")
        return str(existing["id"])

    body = twilio_import_payload(
        number=settings.twilio_phone_number or "",
        account_sid=settings.twilio_account_sid or "",
        auth_token=settings.twilio_auth_token,
        name="NovaCRM onboarding",
    )
    created = (await vapi.request("POST", "/phone-number", idempotent=False, json=body)).json()
    print(f"Imported Twilio number {settings.twilio_phone_number}")
    return str(created["id"])


async def ensure_assistant(vapi: HttpService, settings: Settings) -> str:
    body = assistant_payload(settings)
    assistant_id = settings.vapi_assistant_id
    if not assistant_id:
        assistants: list[dict[str, Any]] = (await vapi.request("GET", "/assistant")).json()
        match = next((a for a in assistants if a.get("name") == settings.vapi_assistant_name), None)
        assistant_id = str(match["id"]) if match else None

    if assistant_id:
        await vapi.request("PATCH", f"/assistant/{assistant_id}", json=body)
        print(f"Updated assistant {assistant_id}")
        return assistant_id
    created = (await vapi.request("POST", "/assistant", idempotent=False, json=body)).json()
    print(f"Created assistant {created['id']}")
    return str(created["id"])


async def main() -> int:
    settings = get_settings()
    try:
        settings.require("vapi_api_key", "public_base_url", "vapi_webhook_secret")
        assert settings.vapi_api_key is not None  # noqa: S101  # checked by require()
        vapi = HttpService.create(
            settings,
            name="vapi",
            base_url=settings.vapi_base_url,
            headers={"Authorization": f"Bearer {settings.vapi_api_key.get_secret_value()}"},
        )
        try:
            phone_number_id = await ensure_phone_number(vapi, settings)
            assistant_id = await ensure_assistant(vapi, settings)
        finally:
            await vapi.aclose()
    except AppError as exc:
        print(f"Setup failed: {exc}")
        return 1

    print("\nAdd these to .env:")
    print(f"VAPI_ASSISTANT_ID={assistant_id}")
    print(f"VAPI_PHONE_NUMBER_ID={phone_number_id}")
    print(f"\nWebhook URL: {webhook_url(settings.public_base_url or '')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
