"""Inbound webhooks from Vapi.

Authentication comes first: the shared secret (sent by Vapi as a static header we set on the
saved assistant) is compared in constant time before the body is read or parsed.
"""

from __future__ import annotations

from typing import Annotated, Any, Final

import structlog
from fastapi import APIRouter, Header, HTTPException, Request, status
from pydantic import ValidationError

from app.api.deps import RuntimeDep, secrets_match
from app.integrations.vapi import WEBHOOK_SECRET_HEADER, VapiWebhook
from app.orchestrator import voice

log = structlog.get_logger(__name__)
router = APIRouter()

MAX_BODY_BYTES: Final = 2_000_000  # transcripts are long, but not this long


@router.post("/webhooks/vapi")
async def vapi_webhook(
    request: Request,
    rt: RuntimeDep,
    secret: Annotated[str | None, Header(alias=WEBHOOK_SECRET_HEADER)] = None,
) -> dict[str, Any]:
    if not secrets_match(secret, rt.settings.vapi_webhook_secret):
        log.warning("vapi_webhook_unauthorised", client=request.client and request.client.host)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid webhook secret")

    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "payload too large")
    try:
        payload = VapiWebhook.model_validate_json(body)
    except ValidationError as exc:
        log.warning("vapi_webhook_invalid", errors=exc.error_count())
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid payload") from exc

    message = payload.message
    call_id = message.call.id if message.call else None
    with structlog.contextvars.bound_contextvars(vapi_call_id=call_id):
        match message.type:
            case "tool-calls":
                return await voice.record_tool_calls(rt, message)
            case "end-of-call-report":
                await voice.record_end_of_call(rt, message)
            case "status-update":
                log.info("vapi_call_status", status=message.status)
            case _:
                log.debug("vapi_message_ignored", type=message.type)
    return {}
