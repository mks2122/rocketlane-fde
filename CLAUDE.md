# CLAUDE.md — NovaCRM Onboarding Agents (Rocketlane FDE take-home)

Multi-agent system that automates customer onboarding for a fictional CRM vendor, NovaCRM:
Gmail deal email → Intake Agent parses + validates → Vapi voice call confirms plan tier with the AE →
Rocketlane project created from the right template → Communication Agent opens a personalized Slack channel.
Overdue-task escalation is a **Rocketlane automation configured in the UI**, not code.

- Assignment brief: `docs/Rocketlane-FDE_Candidate_Assignment.pdf`
- Architecture (canonical, keep in sync): `docs/architecture.md` — visual version: `docs/architecture.html`
- Engineering standards (mandatory): `docs/engineering-standards.md`
- Open questions & assumptions: `docs/open-questions.md`
- Deadline: **Fri 30 Oct 2026**

## Priorities — in this order

1. **Code quality.** Clean, typed, readable, tested, no holes. A smaller feature set done properly beats a larger one done sloppily.
2. **Functionality.** It must actually work end to end on real services, including every failure path.
3. Speed comes after both. Never trade 1 or 2 for it without asking the user.

"No holes" means: every external call has a timeout, bounded retries and a defined failure state; every input is validated at the boundary; every token can expire and is refreshed; every webhook is authenticated and idempotent; every state change is logged. Read `docs/engineering-standards.md` before writing code.

## Stack

Python 3.12 · uv · FastAPI + Uvicorn · OpenAI Agents SDK (`openai-agents`) · Pydantic v2 + pydantic-settings ·
SQLModel on SQLite · APScheduler (SQLAlchemy job store) · httpx (async) + tenacity · structlog ·
Gmail API · Vapi (+ Twilio number) · Rocketlane REST API · slack_sdk · pytest + pytest-asyncio + respx + freezegun · ruff · pyright.

## Commands

```bash
uv sync                                   # install
uv run uvicorn app.main:app --reload      # run API + scheduler
uv run python scripts/gmail_auth.py       # one-time Gmail OAuth -> secrets/token.json
uv run python scripts/setup_vapi_assistant.py
ngrok http --domain=$NGROK_DOMAIN 8000    # expose /webhooks/vapi
uv run pytest                             # offline suite (live tests skipped)
uv run pytest -m live                     # hits real OpenAI
sh scripts/check.sh                       # all checks, stops at the first failure
```

## Architecture rules (non-negotiable)

- **Deterministic spine, LLM at the edges.** `app/orchestrator/state_machine.py` owns the workflow. LLMs only parse email, verify the call transcript, and write Slack copy. An LLM never decides whether to create a project.
- **Agents propose, code executes.** Agents return Pydantic `output_type`s and have **no side-effect tools**. Code validates the output, then calls external APIs.
- **Ports & adapters.** Every integration implements a `Protocol` in `app/ports.py`. Business logic depends on ports, never on SDKs. Fakes live in `app/integrations/fakes.py`.
- **State transitions only via `transition()`.** Never assign `deal.status` directly, and always use the deal it returns. Side-effecting steps assert the required state (e.g. `create_project` requires `TIER_CONFIRMED`).
- **Never guess.** Missing data → `NEEDS_INFO`. Unclear tier → retry → `ESCALATED`. No default tier, ever.
- **Every orchestrator step runs inside `audit_step(...)`** (`app/services/audit.py`) and an Agents SDK `trace(..., group_id=deal_id)`. Success rows commit with the step's transaction; error rows are written in a separate transaction so they survive the rollback.
- Email bodies and call transcripts are **untrusted data**, wrapped in delimiters in prompts.

## Layout

```
app/
  main.py config.py domain.py errors.py db.py models.py ports.py log.py tracing.py scheduler.py
  orchestrator/  state_machine.py workflow.py
  agents/        intake.py tier_verifier.py comms.py validation.py prompts/*.md
  integrations/  gmail.py vapi.py rocketlane.py slack.py notifier.py fakes.py
  services/      audit.py escalation.py routing.py
  api/           webhooks.py deals.py
config/          templates.yaml ae_directory.yaml
scripts/         gmail_auth.py setup_vapi_assistant.py send_test_email.py
tests/           unit/ integration/ fixtures/emails/ conftest.py
secrets/         credentials.json token.json   (git-ignored)
```

## Working agreements

- Verify external API shapes (Rocketlane, Vapi, Slack, Gmail) against official docs before coding against them. If unverified, say so and isolate it in the adapter.
- Pending decisions (Karthik's answers) are **config values**, not code branches. See `docs/open-questions.md`.
- Every behaviour change ships with tests. `uv run pytest`, ruff and pyright must be clean before calling anything done.
- Never commit secrets. Never log tokens, API keys, or full email bodies at INFO.
- Don't commit or push unless the user asks.
- When the architecture changes, update `docs/architecture.md` (and the HTML if the change is significant) in the same piece of work.
