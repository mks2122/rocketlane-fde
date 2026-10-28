# NovaCRM Onboarding Agents

Multi-agent onboarding automation built for the Rocketlane Forward Deployed Engineer take-home.

A deal-closed email arrives in Gmail. The Intake Agent parses and validates it, then a voice agent phones the AE to confirm the plan tier. The system creates the Rocketlane project from the matching template, and the Communication Agent opens a personalized Slack channel. Every step is guarded, retried within limits, audited and traced.

> Status: in development. Setup and run instructions will be completed as each phase lands.

## Docs

- [Architecture](docs/architecture.md) ([visual version](docs/architecture.html))
- [Engineering standards](docs/engineering-standards.md)
- [Open questions & assumptions](docs/open-questions.md)

## Quick start (to be finalized)

```bash
uv sync
cp .env.example .env            # fill in keys
uv run python scripts/gmail_auth.py
uv run uvicorn app.main:app --reload
uv run pytest
```
