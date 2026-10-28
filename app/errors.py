"""Domain error hierarchy.

Adapters translate SDK/HTTP exceptions into these so the orchestrator can decide
between retrying, escalating, or failing fast without knowing about any vendor SDK.
"""


class AppError(Exception):
    """Base class for all application errors."""


class ConfigError(AppError):
    """Required configuration is missing or invalid."""


class IntegrationError(AppError):
    """An external system call failed."""


class RetryableError(IntegrationError):
    """Transient failure (network, timeout, 5xx, 429). Safe to retry."""

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        # Seconds the provider asked us to wait (Retry-After), if it said.
        self.retry_after = retry_after


class OutcomeUnknownError(RetryableError):
    """A non-idempotent request may or may not have been applied (e.g. read timeout).

    Retrying blindly could create a duplicate. The caller must first look for its own
    marker (e.g. the deal id on a Rocketlane project) and only then retry.
    """


class PermanentError(IntegrationError):
    """Non-transient failure (4xx, invalid config). Retrying will not help."""


class AuthorizationError(PermanentError):
    """Our credentials for a provider are missing, revoked or expired. Needs a human."""


class ValidationFailedError(AppError):
    """Inbound data (email, webhook, LLM output) failed validation."""


class IllegalTransitionError(AppError):
    """A deal state change not permitted by the state machine."""


class StaleDealError(AppError):
    """The deal changed since it was read (optimistic lock conflict)."""


class NotFoundError(AppError):
    """The requested entity (e.g. a deal) does not exist."""
