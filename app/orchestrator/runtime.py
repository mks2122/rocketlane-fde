"""The dependencies workflow steps run with.

Scheduled jobs are stored as "module:function" references with plain-string arguments,
so they can't carry objects. When a job fires, it fetches the process's Runtime from
here. The app sets it once at startup; tests set one built from fakes.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.config import AEDirectory, Settings, TemplatesConfig
from app.db import Database
from app.ports import (
    ChatWorkspace,
    CommsWriter,
    IntakeExtractor,
    JobScheduler,
    Mailbox,
    ProjectStore,
    TierVerifier,
    VoiceCaller,
)


@dataclass(frozen=True)
class Runtime:
    settings: Settings
    db: Database
    voice: VoiceCaller
    tier_verifier: TierVerifier
    jobs: JobScheduler
    # Notification channels; None when not configured. Escalations use whichever exist.
    mailbox: Mailbox | None = None
    chat: ChatWorkspace | None = None
    # Intake needs the mailbox (to read threads and ask the AE) and the AE directory.
    intake: IntakeExtractor | None = None
    ae_directory: AEDirectory | None = None
    # Project creation needs Rocketlane and the tier routing config.
    projects: ProjectStore | None = None
    templates: TemplatesConfig | None = None
    # Writes the Slack copy; without it the fixed template is used.
    comms: CommsWriter | None = None


_current: Runtime | None = None


def set_runtime(runtime: Runtime | None) -> None:
    global _current  # one per process, set at startup
    _current = runtime


def get_runtime() -> Runtime:
    if _current is None:
        raise RuntimeError("runtime not initialised; set_runtime() runs at app startup")
    return _current
