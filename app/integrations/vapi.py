"""Vapi adapter: start outbound calls (implements `VoiceCaller`) and parse its webhooks.

Verified against docs.vapi.ai (Oct 2026):
- `POST /call` with `assistantId`, `phoneNumberId`, `customer.number|name` and
  `assistantOverrides.variableValues|metadata`; the response is a Call with `id`.
- Server messages arrive as `{"message": {"type": ...}}`. `tool-calls` carries
  `toolCallList[{id, function{name, arguments}}]` and expects
  `{"results": [{"name", "toolCallId", "result"}]}`. `end-of-call-report` carries
  `endedReason` and `artifact.transcript`.

Webhook models ignore unknown fields: Vapi adds fields often, and we read only what we use.
"""

from __future__ import annotations

import json
from typing import Any, Final, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.config import Settings
from app.domain import TierAnswer
from app.errors import PermanentError, ValidationFailedError
from app.integrations.http import HttpService
from app.ports import OutboundCall

SERVICE: Final = "vapi"
TOOL_RECORD_TIER: Final = "record_plan_tier"
MAX_TRANSCRIPT_CHARS: Final = 100_000
# Header Vapi sends on every server message; set on the saved assistant by
# scripts/setup_vapi_assistant.py and checked by POST /webhooks/vapi.
WEBHOOK_SECRET_HEADER: Final = "x-vapi-secret"  # noqa: S105  # a header name, not a secret


# --- outbound ---------------------------------------------------------------------


def build_call_request(
    call: OutboundCall, *, assistant_id: str, phone_number_id: str
) -> dict[str, Any]:
    return {
        "assistantId": assistant_id,
        "phoneNumberId": phone_number_id,
        "customer": {"number": call.to_number, "name": call.ae_name},
        "name": f"{call.deal_id} attempt {call.attempt_no}",
        "assistantOverrides": {
            # Rendered into the assistant prompt ({{ae_name}}, {{customer_company}}).
            "variableValues": {"ae_name": call.ae_name, "customer_company": call.customer_company},
            # Echoed back on the call object: lets a webhook find its attempt even if it
            # races the response to POST /call.
            "metadata": {"deal_id": call.deal_id, "attempt_no": call.attempt_no},
        },
    }


class VapiCaller:
    def __init__(self, settings: Settings, *, http: HttpService | None = None) -> None:
        settings.require("vapi_api_key", "vapi_assistant_id", "vapi_phone_number_id")
        assert settings.vapi_api_key is not None  # noqa: S101  # checked by require()
        self._assistant_id = settings.vapi_assistant_id or ""
        self._phone_number_id = settings.vapi_phone_number_id or ""
        self._http = http or HttpService.create(
            settings,
            name=SERVICE,
            base_url=settings.vapi_base_url,
            headers={"Authorization": f"Bearer {settings.vapi_api_key.get_secret_value()}"},
        )

    async def start_call(self, call: OutboundCall) -> str:
        """Place the call. Not idempotent: an ambiguous failure raises OutcomeUnknownError
        rather than risk ringing the AE twice."""
        body = build_call_request(
            call, assistant_id=self._assistant_id, phone_number_id=self._phone_number_id
        )
        response = await self._http.request("POST", "/call", idempotent=False, json=body)
        call_id = response.json().get("id")
        if not isinstance(call_id, str) or not call_id:
            raise PermanentError(f"{SERVICE}: POST /call returned no call id")
        return call_id

    async def aclose(self) -> None:
        await self._http.aclose()


# --- inbound webhooks -------------------------------------------------------------


class _Lenient(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class VapiFunction(_Lenient):
    name: str
    arguments: dict[str, Any] | str = Field(default_factory=dict[str, Any])


class VapiToolCall(_Lenient):
    id: str
    function: VapiFunction


class VapiCall(_Lenient):
    id: str
    assistantOverrides: dict[str, Any] | None = None  # noqa: N815  # Vapi's field name

    def deal_ref(self) -> tuple[str, int] | None:
        """(deal_id, attempt_no) from the metadata we sent with POST /call, if echoed."""
        overrides: dict[str, Any] = self.assistantOverrides or {}
        metadata = overrides.get("metadata")
        if not isinstance(metadata, dict):
            return None
        fields = cast(dict[str, object], metadata)
        deal_id, attempt_no = fields.get("deal_id"), fields.get("attempt_no")
        if isinstance(deal_id, str) and type(attempt_no) is int:  # bool is an int subclass
            return deal_id, attempt_no
        return None


class VapiArtifact(_Lenient):
    transcript: str | None = None
    recordingUrl: str | None = None  # noqa: N815
    recording: dict[str, Any] | None = None

    def recording_link(self) -> str | None:
        if self.recordingUrl:
            return self.recordingUrl
        for key in ("stereoUrl", "monoUrl", "url"):
            value = (self.recording or {}).get(key)
            if isinstance(value, str) and value:
                return value
        return None


class VapiMessage(_Lenient):
    type: str
    call: VapiCall | None = None
    toolCallList: list[VapiToolCall] = Field(default_factory=list[VapiToolCall])  # noqa: N815
    endedReason: str | None = None  # noqa: N815
    artifact: VapiArtifact | None = None
    status: str | None = None


class VapiWebhook(_Lenient):
    message: VapiMessage


class RecordPlanTierArgs(BaseModel):
    """Arguments of the assistant's `record_plan_tier` tool call."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    tier: TierAnswer
    ae_confirmed: bool
    ae_quote: str = Field(default="", max_length=500)


def parse_tool_args(arguments: dict[str, Any] | str) -> RecordPlanTierArgs:
    try:
        data = json.loads(arguments) if isinstance(arguments, str) else arguments
        return RecordPlanTierArgs.model_validate(data)
    except (json.JSONDecodeError, ValidationError) as exc:
        raise ValidationFailedError(f"invalid {TOOL_RECORD_TIER} arguments: {exc}") from exc


def tool_result(call: VapiToolCall, result: str) -> dict[str, str]:
    return {"name": call.function.name, "toolCallId": call.id, "result": result}


class UnconfiguredVoice:
    """Stand-in while Vapi isn't configured: every call fails with a clear, permanent reason."""

    async def start_call(self, call: OutboundCall) -> str:
        raise PermanentError(
            "voice calls are disabled: set VAPI_API_KEY, VAPI_ASSISTANT_ID and VAPI_PHONE_NUMBER_ID"
        )
