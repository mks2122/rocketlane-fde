"""In-memory fakes for every port, used by tests and offline demo runs.

They behave like the real systems in the ways the orchestrator relies on (ids,
idempotent mark/invite, channel-name uniqueness) and can be told to fail:

    projects.fail("create_project", RetryableError("503"), OutcomeUnknownError("timeout"))

queues errors that the next calls of that operation raise, in order. Every call is
appended to `calls` as (operation, argument) so tests can assert on side effects.
"""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import count
from typing import Any

from app.errors import PermanentError
from app.ports import (
    ChannelRef,
    ChatWorkspace,
    DealIntake,
    InboundEmail,
    IntakeExtractor,
    JobScheduler,
    Mailbox,
    NewProject,
    OutboundCall,
    OutboundEmail,
    ProjectRef,
    ProjectStore,
    TierVerdict,
    TierVerifier,
    VoiceCaller,
)

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class _Fake:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self._faults: defaultdict[str, deque[Exception]] = defaultdict(deque)
        self._ids = count(1)

    def fail(self, operation: str, *errors: Exception) -> None:
        """Make the next len(errors) calls of `operation` raise these errors, in order."""
        self._faults[operation].extend(errors)

    def _enter(self, operation: str, argument: Any = None) -> None:
        self.calls.append((operation, argument))
        if self._faults[operation]:
            raise self._faults[operation].popleft()

    def _next_id(self, prefix: str) -> str:
        return f"{prefix}{next(self._ids)}"

    def called(self, operation: str) -> list[Any]:
        return [arg for op, arg in self.calls if op == operation]


class FakeMailbox(_Fake):
    def __init__(self) -> None:
        super().__init__()
        self.inbox: dict[str, InboundEmail] = {}
        self.processed: set[str] = set()
        self.sent: list[OutboundEmail] = []
        # Our sent replies as they appear on their threads (from_self=True).
        self.outbox: list[InboundEmail] = []
        self.address = "cs-inbox@novacrm-demo.com"
        self._broken: dict[str, Exception] = {}

    def deliver(self, email: InboundEmail) -> None:
        """Test helper: an email arrives in the inbox."""
        self.inbox[email.message_id] = email

    def break_message(self, message_id: str, error: Exception) -> None:
        """Test helper: every fetch of this one message raises `error`."""
        self._broken[message_id] = error

    async def list_new_message_ids(self) -> list[str]:
        self._enter("list_new_message_ids")
        new = (e for e in self.inbox.values() if e.message_id not in self.processed)
        return [e.message_id for e in sorted(new, key=lambda e: e.received_at)]

    async def fetch(self, message_id: str) -> InboundEmail:
        self._enter("fetch", message_id)
        if message_id in self._broken:
            raise self._broken[message_id]
        try:
            return self.inbox[message_id]
        except KeyError:
            raise PermanentError(f"fake mailbox: no message {message_id}") from None

    async def fetch_thread(self, thread_id: str) -> list[InboundEmail]:
        self._enter("fetch_thread", thread_id)
        thread = [e for e in [*self.inbox.values(), *self.outbox] if e.thread_id == thread_id]
        return sorted(thread, key=lambda e: e.received_at)

    async def mark_processed(self, message_id: str) -> None:
        self._enter("mark_processed", message_id)
        self.processed.add(message_id)

    async def send(self, email: OutboundEmail) -> str:
        self._enter("send", email)
        self.sent.append(email)
        sent_id = self._next_id("sent_")
        if email.thread_id is not None:
            on_thread = [
                e.received_at
                for e in [*self.inbox.values(), *self.outbox]
                if e.thread_id == email.thread_id
            ]
            self.outbox.append(
                InboundEmail(
                    message_id=sent_id,
                    thread_id=email.thread_id,
                    from_email=self.address,
                    subject=email.subject,
                    body_text=email.body_text,
                    received_at=max(on_thread, default=_EPOCH) + timedelta(seconds=1),
                    from_self=True,
                )
            )
        return sent_id


class FakeVoiceCaller(_Fake):
    def __init__(self) -> None:
        super().__init__()
        self.placed: list[OutboundCall] = []

    async def start_call(self, call: OutboundCall) -> str:
        self._enter("start_call", call)
        self.placed.append(call)
        return self._next_id("call_")


class FakeProjectStore(_Fake):
    def __init__(self) -> None:
        super().__init__()
        self.projects: dict[str, ProjectRef] = {}
        self.created: list[NewProject] = []
        self._lost_response: Exception | None = None

    def seed(self, project: ProjectRef) -> None:
        """Test helper: a project that already exists (e.g. created by hand)."""
        self.projects[project.project_id] = project

    async def list_projects(self) -> list[ProjectRef]:
        self._enter("list_projects")
        return list(self.projects.values())

    async def create_project(self, project: NewProject) -> ProjectRef:
        self._enter("create_project", project)
        ref = self._store(project)
        if self._lost_response is not None:
            error, self._lost_response = self._lost_response, None
            raise error  # created upstream, but the caller never hears about it
        return ref

    def lose_next_response(self, error: Exception) -> None:
        """Test helper: the next create succeeds in Rocketlane, but its response is lost."""
        self._lost_response = error

    def _store(self, project: NewProject) -> ProjectRef:
        ref = ProjectRef(
            project_id=self._next_id("proj_"),
            name=project.project_name,
            customer_company=project.customer_company,
            deal_marker=project.deal_id,
        )
        self.projects[ref.project_id] = ref
        self.created.append(project)
        return ref


class FakeChatWorkspace(_Fake):
    def __init__(self) -> None:
        super().__init__()
        self.channels: dict[str, ChannelRef] = {}
        self.topics: dict[str, str] = {}
        self.messages: defaultdict[str, list[str]] = defaultdict(list)
        self.members: defaultdict[str, set[str]] = defaultdict(set)
        self.external_invites: defaultdict[str, set[str]] = defaultdict(set)

    async def find_channel(self, name: str) -> ChannelRef | None:
        self._enter("find_channel", name)
        return self.channels.get(name)

    async def create_channel(self, name: str) -> ChannelRef:
        self._enter("create_channel", name)
        if name in self.channels:
            # Mirrors Slack's `name_taken`.
            raise PermanentError(f"fake chat: channel name taken: {name}")
        ref = ChannelRef(channel_id=self._next_id("C"), name=name)
        self.channels[name] = ref
        return ref

    async def set_topic(self, channel_id: str, topic: str) -> None:
        self._enter("set_topic", (channel_id, topic))
        self.topics[channel_id] = topic

    async def post_message(self, channel_id: str, text: str) -> str:
        self._enter("post_message", (channel_id, text))
        self.messages[channel_id].append(text)
        return self._next_id("ts_")

    async def invite_members(self, channel_id: str, emails: Sequence[str]) -> None:
        self._enter("invite_members", (channel_id, tuple(emails)))
        self.members[channel_id].update(e.lower() for e in emails)

    async def invite_external(self, channel_id: str, email: str) -> None:
        self._enter("invite_external", (channel_id, email))
        self.external_invites[channel_id].add(email.lower())


class FakeIntakeExtractor(_Fake):
    """Returns queued intakes in order; fails the test if asked more often than scripted."""

    def __init__(self, *intakes: DealIntake) -> None:
        super().__init__()
        self.intakes: deque[DealIntake] = deque(intakes)

    async def extract(self, *, thread_text: str) -> DealIntake:
        self._enter("extract", thread_text)
        if not self.intakes:
            raise AssertionError("FakeIntakeExtractor called with no intake queued")
        return self.intakes.popleft()


class FakeTierVerifier(_Fake):
    """Returns queued verdicts in order; fails the test if asked more often than scripted."""

    def __init__(self, *verdicts: TierVerdict) -> None:
        super().__init__()
        self.verdicts: deque[TierVerdict] = deque(verdicts)

    async def verify(self, *, transcript: str, customer_company: str) -> TierVerdict:
        self._enter("verify", (transcript, customer_company))
        if not self.verdicts:
            raise AssertionError("FakeTierVerifier called with no verdict queued")
        return self.verdicts.popleft()


@dataclass(frozen=True)
class ScheduledJob:
    job: str
    run_at: datetime
    args: tuple[str, ...]
    job_id: str


class FakeJobScheduler:
    """Records jobs instead of running them; tests run them explicitly. Same-id replaces."""

    def __init__(self) -> None:
        self.jobs: dict[str, ScheduledJob] = {}

    def schedule(self, job: str, *, run_at: datetime, args: Sequence[str], job_id: str) -> None:
        self.jobs[job_id] = ScheduledJob(job=job, run_at=run_at, args=tuple(args), job_id=job_id)

    def pop(self, job_id: str) -> ScheduledJob:
        return self.jobs.pop(job_id)

    def has_pending(self, job_id_prefix: str) -> bool:
        return any(job_id.startswith(job_id_prefix) for job_id in self.jobs)


# Static conformance: pyright fails here if a fake drifts from its port.
_mailbox: type[Mailbox] = FakeMailbox
_voice: type[VoiceCaller] = FakeVoiceCaller
_projects: type[ProjectStore] = FakeProjectStore
_chat: type[ChatWorkspace] = FakeChatWorkspace
_verifier: type[TierVerifier] = FakeTierVerifier
_intake: type[IntakeExtractor] = FakeIntakeExtractor
_jobs: type[JobScheduler] = FakeJobScheduler
