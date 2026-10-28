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


class PermanentError(IntegrationError):
    """Non-transient failure (4xx, invalid config). Retrying will not help."""


class ValidationFailedError(AppError):
    """Inbound data (email, webhook, LLM output) failed validation."""


class IllegalTransitionError(AppError):
    """A deal state change not permitted by the state machine."""


class StaleDealError(AppError):
    """The deal changed since it was read (optimistic lock conflict)."""
