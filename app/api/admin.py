"""Admin endpoints (X-Admin-Token).

`POST /admin/test-deals` exists so the voice step can be exercised before intake (P6) is
built. It is disabled in prod.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.api.deps import AdminDep, RuntimeDep
from app.config import E164_PATTERN
from app.domain import DealStatus
from app.models import Deal, utcnow
from app.orchestrator.state_machine import transition
from app.orchestrator.voice import schedule_call_attempt

router = APIRouter(prefix="/admin")


class TestDealRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    customer_company: str = Field(min_length=1, max_length=200)
    ae_name: str = Field(min_length=1, max_length=200)
    ae_email: EmailStr
    ae_phone: str = Field(pattern=E164_PATTERN)


@router.post("/test-deals", status_code=status.HTTP_201_CREATED)
async def create_test_deal(body: TestDealRequest, rt: RuntimeDep, _: AdminDep) -> dict[str, Any]:
    if rt.settings.env == "prod":
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    marker = f"manual-{uuid4().hex[:12]}"
    async with rt.db.session() as session:
        deal = Deal(
            gmail_message_id=marker,
            gmail_thread_id=marker,
            sender_email=body.ae_email,
            customer_company=body.customer_company,
            ae_name=body.ae_name,
            ae_email=body.ae_email,
            ae_phone=body.ae_phone,
        )
        session.add(deal)
        await session.flush()
        deal = await transition(
            session,
            deal,
            DealStatus.AWAITING_TIER,
            actor="admin",
            rationale="test deal created through /admin/test-deals",
        )
    run_at = schedule_call_attempt(rt, deal.id, earliest=utcnow(), attempt_no=1)
    return {"deal_id": deal.id, "status": deal.status, "first_call_at": run_at.isoformat()}
