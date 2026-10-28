"""Deal lifecycle. The only way a deal's status may change is `transition()`.

`transition()` enforces the allowed-transition table, applies optimistic locking on
`Deal.version`, and writes the audit row in the same transaction as the change.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from sqlalchemy import update
from sqlmodel.ext.asyncio.session import AsyncSession

from app.domain import DealStatus as S
from app.errors import IllegalTransitionError, StaleDealError
from app.models import Deal, utcnow
from app.services.audit import record

ALLOWED: Final[Mapping[S, frozenset[S]]] = {
    S.RECEIVED: frozenset({S.IGNORED, S.NEEDS_INFO, S.AWAITING_TIER, S.ESCALATED}),
    S.NEEDS_INFO: frozenset({S.RECEIVED, S.ESCALATED}),
    S.AWAITING_TIER: frozenset({S.CALLING, S.CALL_RETRY_WAIT, S.ESCALATED}),
    S.CALLING: frozenset({S.TIER_CONFIRMED, S.CALL_RETRY_WAIT, S.ESCALATED}),
    S.CALL_RETRY_WAIT: frozenset({S.CALLING, S.ESCALATED}),
    S.TIER_CONFIRMED: frozenset(
        {S.DUPLICATE_HOLD, S.RL_RETRY_WAIT, S.PROJECT_CREATED, S.ESCALATED}
    ),
    S.RL_RETRY_WAIT: frozenset({S.TIER_CONFIRMED, S.ESCALATED}),
    S.DUPLICATE_HOLD: frozenset({S.ESCALATED}),
    S.PROJECT_CREATED: frozenset({S.COMPLETED, S.PARTIAL}),
    S.PARTIAL: frozenset({S.PROJECT_CREATED, S.ESCALATED}),
    # Human resolution re-enters the flow at the step that was blocked.
    S.ESCALATED: frozenset({S.RECEIVED, S.AWAITING_TIER, S.TIER_CONFIRMED, S.PROJECT_CREATED}),
    S.IGNORED: frozenset(),
    S.COMPLETED: frozenset(),
}

TERMINAL: Final[frozenset[S]] = frozenset(s for s, targets in ALLOWED.items() if not targets)

# Fields a transition may set alongside the status. Identity and bookkeeping
# columns are deliberately excluded.
_PROTECTED_FIELDS: Final[frozenset[str]] = frozenset(
    {"id", "status", "version", "gmail_message_id", "created_at", "updated_at"}
)


def can_transition(current: S, target: S) -> bool:
    return target in ALLOWED[current]


async def transition(
    session: AsyncSession,
    deal: Deal,
    target: S,
    *,
    actor: str,
    rationale: str,
    changes: Mapping[str, Any] | None = None,
) -> Deal:
    """Move `deal` to `target`, optionally updating other fields atomically.

    Returns the updated deal as loaded in `session`; use the return value, not the
    instance that was passed in.

    Raises IllegalTransitionError if the move is not allowed, and StaleDealError if
    the deal was modified since it was loaded (the caller should reload and decide again).
    The caller's unit of work commits both the change and its audit row.
    """
    current = deal.status
    if not can_transition(current, target):
        raise IllegalTransitionError(f"{deal.id}: {current} -> {target} is not allowed")

    extra = dict(changes or {})
    bad = sorted(k for k in extra if k in _PROTECTED_FIELDS or k not in Deal.model_fields)
    if bad:
        raise ValueError(f"transition cannot set fields: {', '.join(bad)}")

    stmt = (
        update(Deal)
        .where(Deal.id == deal.id, Deal.version == deal.version)  # pyright: ignore[reportArgumentType]
        .values(status=target, version=deal.version + 1, updated_at=utcnow(), **extra)
    )
    result = await session.exec(stmt)  # pyright: ignore[reportCallIssue, reportArgumentType]
    if result.rowcount != 1:
        raise StaleDealError(f"{deal.id}: modified concurrently (expected version {deal.version})")

    record(
        session,
        deal_id=deal.id,
        actor=actor,
        action="transition",
        input={"from": current, "to": target, "changes": extra},
        output={"version": deal.version + 1},
        rationale=rationale,
    )
    # The caller's instance may belong to another session; always return the row as
    # it now exists in this one.
    fresh = await session.get(Deal, deal.id, populate_existing=True)
    if fresh is None:  # pragma: no cover - the UPDATE above just matched this row
        raise StaleDealError(f"{deal.id}: disappeared during transition")
    return fresh
