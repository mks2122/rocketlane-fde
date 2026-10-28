"""Payloads for provisioning Vapi: the saved assistant and the imported Twilio number.

Used by `scripts/setup_vapi_assistant.py`. The assistant is saved in Vapi (not sent
per call) because Vapi attaches our server headers only to server URLs that come from a
saved resource.

⚠ Partly unverified: the `model.tools` and `endCall` tool shapes and the phone-number
import fields follow Vapi's published examples but weren't checked against the full API
reference. A mismatch surfaces as a 4xx with Vapi's message when the script runs.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Final

from pydantic import SecretStr

from app.config import Settings
from app.domain import TierAnswer
from app.integrations.vapi import TOOL_RECORD_TIER, WEBHOOK_SECRET_HEADER

PROMPT_PATH: Final = (
    Path(__file__).resolve().parent.parent / "agents" / "prompts" / "vapi_assistant.md"
)
SERVER_MESSAGES: Final = ["tool-calls", "end-of-call-report", "status-update"]
FIRST_MESSAGE: Final = (
    "Hi, this is the NovaCRM onboarding assistant calling about the new {{customer_company}} "
    "deal. Am I speaking with {{ae_name}}?"
)

RECORD_TIER_TOOL: Final[dict[str, Any]] = {
    "type": "function",
    "function": {
        "name": TOOL_RECORD_TIER,
        "description": (
            "Record which NovaCRM plan the customer bought, after reading it back to the AE. "
            "Call exactly once, at the end of the conversation."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "tier": {
                    "type": "string",
                    "enum": [t.value for t in TierAnswer],
                    "description": "enterprise or growth only if the AE named and confirmed it.",
                },
                "ae_confirmed": {
                    "type": "boolean",
                    "description": "True only if the AE explicitly confirmed the read-back.",
                },
                "ae_quote": {
                    "type": "string",
                    "description": "The AE's own words naming the plan.",
                },
            },
            "required": ["tier", "ae_confirmed", "ae_quote"],
        },
    },
}


def webhook_url(public_base_url: str) -> str:
    return f"{public_base_url.rstrip('/')}/webhooks/vapi"


def assistant_payload(settings: Settings, *, prompt: str | None = None) -> dict[str, Any]:
    settings.require("public_base_url", "vapi_webhook_secret")
    assert settings.public_base_url is not None  # noqa: S101  # checked by require()
    assert settings.vapi_webhook_secret is not None  # noqa: S101
    return {
        "name": settings.vapi_assistant_name,
        "firstMessage": FIRST_MESSAGE,
        "model": {
            "provider": "openai",
            "model": settings.vapi_model,
            "temperature": 0.2,
            "messages": [
                {"role": "system", "content": prompt or PROMPT_PATH.read_text(encoding="utf-8")}
            ],
            "tools": [RECORD_TIER_TOOL, {"type": "endCall"}],
        },
        "server": {
            "url": webhook_url(settings.public_base_url),
            "headers": {WEBHOOK_SECRET_HEADER: settings.vapi_webhook_secret.get_secret_value()},
            "timeoutSeconds": 20,
        },
        "serverMessages": SERVER_MESSAGES,
        "maxDurationSeconds": settings.call_max_duration_seconds,
        "endCallMessage": "Thanks for your time. Goodbye!",
    }


def twilio_import_payload(
    *, number: str, account_sid: str, auth_token: SecretStr, name: str
) -> dict[str, Any]:
    return {
        "provider": "twilio",
        "number": number,
        "twilioAccountSid": account_sid,
        "twilioAuthToken": auth_token.get_secret_value(),
        "name": name,
    }
