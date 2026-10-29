"""Ports: the interfaces business logic depends on.

Each external system is reached through one Protocol here. Real adapters live in
`app/integrations/`, in-memory fakes in `app/integrations/fakes.py`. Adapters raise
only domain errors from `app/errors.py` (RetryableError, OutcomeUnknownError,
PermanentError), never SDK or HTTP exceptions, so the orchestrator can decide between
retrying, escalating and failing without knowing which vendor is behind a port.

Values crossing a port are frozen Pydantic models: validated once at the boundary,
then immutable.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime
from typing import Protocol

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.config import E164_PATTERN
from app.domain import PlanTier, TierAnswer


class _Value(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# --- Mail (Gmail) -------------------------------------------------------------


class InboundEmail(_Value):
    message_id: str = Field(min_length=1)  # provider id; the deal's natural key
    thread_id: str = Field(min_length=1)
    rfc822_message_id: str | None = None  # Message-ID header, for In-Reply-To threading
    from_email: EmailStr
    from_name: str | None = None
    subject: str
    body_text: str
    received_at: datetime
    # True for messages this mailbox sent (e.g. our clarification questions). Intake
    # must never extract deal data from our own words.
    from_self: bool = False


class OutboundEmail(_Value):
    to: tuple[EmailStr, ...] = Field(min_length=1)
    subject: str = Field(min_length=1)
    body_text: str = Field(min_length=1)
    # Set both to reply on an existing thread.
    thread_id: str | None = None
    in_reply_to: str | None = None


class Mailbox(Protocol):
    async def list_new_message_ids(self) -> list[str]:
        """Ids of inbound messages not yet marked processed, oldest first."""
        ...

    async def fetch(self, message_id: str) -> InboundEmail: ...

    async def fetch_thread(self, thread_id: str) -> list[InboundEmail]:
        """Every readable message on the thread, oldest first (ours marked `from_self`)."""
        ...

    async def mark_processed(self, message_id: str) -> None:
        """Idempotent: marking an already-processed message is a no-op."""
        ...

    async def send(self, email: OutboundEmail) -> str:
        """Send and return the provider message id."""
        ...


# --- Intake (LLM) --------------------------------------------------------------


class ExtractedField(_Value):
    value: str | None = Field(
        description="The value exactly as the AE wrote it, or null if the AE didn't state it."
    )
    evidence: str | None = Field(
        description="A verbatim quote from the AE's email that contains the value, or null."
    )


class DealIntake(_Value):
    """What the Intake Agent read from a deal email thread. Code checks it before use."""

    is_deal_handoff: bool = Field(
        description="True if the AE is handing over a closed deal for onboarding."
    )
    customer_company: ExtractedField
    contact_name: ExtractedField = Field(description="The customer's main contact person.")
    contact_email: ExtractedField = Field(description="The customer contact's email address.")
    ae_name: ExtractedField = Field(description="The Account Executive's name.")
    ae_phone: ExtractedField = Field(description="The AE's phone number, if they gave one.")
    sf_opportunity_url: ExtractedField = Field(description="Link to the Salesforce opportunity.")
    notes: str | None = Field(description="Anything else onboarding should know, or null.")


class IntakeExtractor(Protocol):
    async def extract(self, *, thread_text: str) -> DealIntake:
        """Raises IntegrationError if the model is unavailable or returns invalid output."""
        ...


# --- Voice (Vapi) -------------------------------------------------------------


class OutboundCall(_Value):
    deal_id: str = Field(min_length=1)
    attempt_no: int = Field(ge=1)
    to_number: str = Field(pattern=E164_PATTERN)
    ae_name: str = Field(min_length=1)
    customer_company: str = Field(min_length=1)


class VoiceCaller(Protocol):
    async def start_call(self, call: OutboundCall) -> str:
        """Place the call and return the provider call id. The result arrives by webhook."""
        ...


class TierVerdict(_Value):
    """The Tier Verifier agent's independent reading of a call transcript."""

    tier: TierAnswer
    ae_confirmed: bool = Field(
        description="True only if the AE explicitly confirmed the tier when it was read back."
    )
    evidence_quote: str = Field(
        description="The AE's own words stating or confirming the tier, copied verbatim."
    )
    rationale: str = Field(description="One or two sentences explaining the reading.")


class TierVerifier(Protocol):
    async def verify(self, *, transcript: str, customer_company: str) -> TierVerdict:
        """Raises IntegrationError if the model is unavailable or returns invalid output."""
        ...


# --- Jobs (APScheduler) -------------------------------------------------------


class JobScheduler(Protocol):
    def schedule(self, job: str, *, run_at: datetime, args: Sequence[str], job_id: str) -> None:
        """Durably run `job` (a "module:function" reference) at `run_at`.

        Re-scheduling the same `job_id` replaces the earlier job.
        """
        ...

    def has_pending(self, job_id_prefix: str) -> bool:
        """Whether any not-yet-run job's id starts with `job_id_prefix`."""
        ...


# --- Projects (Rocketlane) ----------------------------------------------------


class ProjectRef(_Value):
    project_id: str = Field(min_length=1)
    name: str
    customer_company: str | None = None
    # Our idempotency marker (the deal id), stored on the project. None for projects
    # this system did not create.
    deal_marker: str | None = None
    is_active: bool = True


class NewProject(_Value):
    deal_id: str = Field(min_length=1)  # stored on the project as the idempotency marker
    project_name: str = Field(min_length=1)
    customer_company: str = Field(min_length=1)
    plan_tier: PlanTier
    template_id: int
    start_date: date  # the template's durations (working days) set the due date
    owner_email: EmailStr
    csm_email: EmailStr
    # Template placeholder the CSM fills, so the CSM's tasks are assigned at creation.
    csm_placeholder_id: int | None = None
    # Set only when the customer contact should be invited (sends them an email).
    customer_contact_email: EmailStr | None = None
    assign_unassigned_tasks_to_owner: bool = False
    # (field id, text value) pairs for optional custom project fields.
    custom_fields: tuple[tuple[int, str], ...] = ()


class ProjectStore(Protocol):
    async def list_projects(self) -> list[ProjectRef]:
        """Every project, across all pages. Duplicate detection runs on this in code."""
        ...

    async def create_project(self, project: NewProject) -> ProjectRef:
        """Not idempotent. On OutcomeUnknownError, check `list_projects` for the marker."""
        ...


# --- Chat (Slack) -------------------------------------------------------------


class ChannelRef(_Value):
    channel_id: str = Field(min_length=1)
    name: str = Field(min_length=1)


class ChatWorkspace(Protocol):
    async def find_channel(self, name: str) -> ChannelRef | None: ...

    async def create_channel(self, name: str) -> ChannelRef: ...

    async def set_topic(self, channel_id: str, topic: str) -> None: ...

    async def post_message(self, channel_id: str, text: str) -> str:
        """Post and return the message timestamp (Slack's message id)."""
        ...

    async def invite_members(self, channel_id: str, emails: Sequence[str]) -> None:
        """Invite internal users by email. Already-a-member is not an error."""
        ...

    async def invite_external(self, channel_id: str, email: str) -> None:
        """Slack Connect invite for the customer (mocked in the demo)."""
        ...
