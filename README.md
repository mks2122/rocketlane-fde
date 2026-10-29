# NovaCRM Onboarding Agents

A multi-agent system that automates NovaCRM's customer onboarding, built for the Rocketlane Forward Deployed Engineer take-home.

```
AE deal email ──► Gmail inbox ──► Intake Agent ──► voice call to the AE ──► Rocketlane project ──► Communication Agent
                                  (parse +          (Vapi + Twilio:          (template chosen        (personalized Slack
                                   validate)         "Enterprise or Growth?") by confirmed tier)      channel + welcome)

Overdue tasks ──► Rocketlane automation (configured in Rocketlane): 1 day → Project Manager, 4 days → Project Owner
```

**Design rule: deterministic spine, LLM at the edges.** A state machine in code owns the workflow. LLM agents only do three things: read the email, check the call transcript, and write the Slack copy. They return typed, validated output and have no tools that change anything. An LLM never decides whether a project gets created.

## What it does

| Brief | Implementation |
|---|---|
| **Agent 1, Intake & Routing** | Polls the Gmail inbox. The Intake Agent (OpenAI Agents SDK) extracts customer, contact, AE, phone and Salesforce link, each with a verbatim quote from the email that code checks. If anything is missing, it emails the AE asking for it rather than guessing. A Vapi voice agent phones the AE to confirm the tier, and a Tier Verifier agent independently re-reads the transcript. The project is created through the Rocketlane API from the tier's template. |
| **Agent 2, Communication** | Opens `#onb-<customer>-<id>`. The Comms Agent writes a tier-specific topic and welcome message (Enterprise: dedicated CSM, 30 days; Growth: onboarding pod, 14 days). Code checks the copy, and a fixed template is used if the agent fails, so the channel never depends on the LLM. |
| **Rocketlane overdue escalation** | A global Rocketlane automation configured in the UI, not code: task overdue 1 day → Project Manager, 4 days → Project Owner, limited to the two onboarding templates. See [screenshot](docs/images/rocketlane-overdue-automation.png). |
| **Data-migration verification** | When "Customer verifies migrated data" is marked Completed, a template automation requests approval from the customer. |

### Guardrails

- **Never guesses.** A missing field puts the deal in `needs_info` and asks the AE (bounded reminders, then a human). There is no default tier.
- **Two independent checks on the tier.** The voice agent records the AE's answer during the call, and a separate agent re-reads the transcript. Both must agree, with an explicit read-back confirmation. Otherwise the call is retried inside a calling window (Mon–Fri 09:00–19:00 IST by default, max 3 attempts), then the deal is escalated to a human by email and in `#cs-escalations`.
- **Untrusted input.** Email bodies and transcripts are wrapped as data in prompts. Extracted values must be quoted from the source. Senders must be on an allowlist (fail closed).
- **Duplicate protection.** An existing active project for the same customer holds the deal and escalates it. A create that times out but succeeded is detected and adopted, not repeated.
- **Every external call** has a timeout, bounded retries with backoff, and a defined failure state. Slack and Rocketlane outages retry on a schedule, then escalate. Webhooks are authenticated (`x-vapi-secret`) and idempotent. The Gmail token is refreshed and persisted.
- **Full audit trail.** Every step writes an audit row (timestamp, actor, input, output, rationale) and an OpenAI Agents SDK trace grouped by deal. Both are available at `GET /deals/{id}/audit` and `/traces`.
- **Human in the loop.** Escalated deals are resolved through `POST /deals/{id}/resolve` (confirm tier, retry call, re-read email, retry channel, dismiss). Each resolution records who and why.

## Stack

Python 3.12 · uv · FastAPI · OpenAI Agents SDK · Pydantic v2 · SQLModel/SQLite · APScheduler (persistent job store) · httpx + tenacity · structlog · Gmail API · Vapi + Twilio · Rocketlane REST API · Slack Web API · pytest/respx · ruff · pyright (strict).

## Setup

### 1. Install

```bash
uv sync
cp .env.example .env
cp config/ae_directory.example.yaml config/ae_directory.yaml
```

Every setting is documented in [`.env.example`](.env.example). `ADMIN_TOKEN` (16+ characters) is required.

### 2. OpenAI

Set `OPENAI_API_KEY`. The default model is `gpt-4.1-mini`.

### 3. Gmail (the CS inbox the agent watches)

1. In Google Cloud, enable the **Gmail API**, create an **OAuth client ID** of type **Desktop app**, and add the inbox account as a **Test user** on the OAuth consent screen.
2. Save the client JSON as `secrets/credentials.json` (git-ignored).
3. Run the one-time consent. It writes `secrets/token.json`:
   ```bash
   uv run python scripts/gmail_auth.py
   ```
4. In `.env`: `GMAIL_ENABLED=true` and `ALLOWED_SENDER_EMAILS=["ae@example.com"]` (or `ALLOWED_SENDER_DOMAINS`). With both empty, polling refuses to start.

### 4. AE directory

`config/ae_directory.yaml` maps each AE's email to a phone number in E.164 format, e.g. `+919876543210`. The call goes to the number on file. A number in the email is only used when the AE isn't listed.

### 5. Rocketlane

1. Create two project templates with the same 4 phases and 15 tasks: **Enterprise Onboarding (30d)** and **Growth Onboarding (14d)**.
2. Put their ids, the CSM placeholder role, CSM pool and project owner in [`config/templates.yaml`](config/templates.yaml).
3. Set `ROCKETLANE_API_KEY` and `ROCKETLANE_APP_URL`, then check the config against the workspace (read-only):
   ```bash
   uv run python scripts/check_rocketlane.py
   ```
4. In the Rocketlane UI, configure:
   - **Settings → Automations → Global automation**, trigger *Task becomes overdue*:
     - Block 1: overdue by 1 day, template is one of the two → notify *Project Manager*.
     - Block 2: overdue by 4 days, same filter → notify *Project → Project Owner*.
   - In each template, on *Customer verifies migrated data*: Automations → *Status changed to Completed* → *Request approval* from *Customer*.

### 6. Voice: Vapi + Twilio

1. Buy a Twilio number and allow outbound calls to the AE's country (**Voice → Geo permissions**).
2. Set `VAPI_API_KEY`, the `TWILIO_*` values, and a random `VAPI_WEBHOOK_SECRET`:
   ```bash
   uv run python -c "import secrets; print(secrets.token_urlsafe(32))"
   ```
3. Expose the app with an ngrok static domain and set `PUBLIC_BASE_URL` to it.
4. Provision the assistant. This is idempotent; copy the printed ids into `.env`:
   ```bash
   uv run python scripts/setup_vapi_assistant.py
   ```

### 7. Slack

1. Create a Slack app with the bot scopes `channels:read`, `channels:manage`, `chat:write`, `users:read`, `users:read.email`. Install it and set `SLACK_BOT_TOKEN`.
2. Create `#cs-escalations` and run `/invite @your-bot` in it.
3. Check (read-only):
   ```bash
   uv run python scripts/check_slack.py
   ```

Inviting the customer into the channel uses Slack Connect, which needs a paid workspace. With `SLACK_CONNECT_ENABLED=false` (the default), that invite is logged instead of sent; everything else is real. In production you set it to `true` on a paid plan and the same code calls `conversations.inviteShared`.

## Run

```bash
uv run uvicorn app.main:create_app --factory                   # API + scheduler + Gmail poller
ngrok http --url=<your-static-domain>.ngrok-free.dev 8000      # Vapi webhooks reach /webhooks/vapi
```

Then email the CS inbox from an allowed AE address, without a plan tier:

> Hi team, we just closed **Globex Corp**!
> Main contact: Hank Scorpio (hank@globex.com).
> SF: https://novacrm.lightning.force.com/lightning/r/Opportunity/006Hs00001AbCdE/view
> Thanks, Karthick Sriram

Within one poll interval (30s), the AE's phone rings. Answer "Enterprise", confirm the read-back, and the project and Slack channel follow.

Rocketlane's built-in sample "[Sample] Acme…" project makes any deal for "Acme" a duplicate, so it is held. Demo with another customer name.

### Watching a deal

All endpoints except `/health` and the Vapi webhook need the header `X-Admin-Token: $ADMIN_TOKEN`. Interactive docs are at `http://localhost:8000/docs`.

| Endpoint | Purpose |
|---|---|
| `GET /health` | Database and Gmail token status |
| `GET /deals`, `GET /deals/{id}` | Deal status and extracted fields |
| `GET /deals/{id}/audit` | Every step: time, actor, input, output, rationale |
| `GET /deals/{id}/traces` | OpenAI Agents SDK traces for the deal |
| `POST /deals/{id}/resolve` | Human decision on an escalated deal: `{"action": "confirm_tier", "plan_tier": "growth", "actor": "priya", "reason": "..."}` |
| `POST /deals/{id}/retry` | Re-run the step that failed |
| `POST /admin/poll` | Poll Gmail now instead of waiting |
| `POST /admin/test-deals` | Start a deal at the call step without an email (not available in prod) |

A deal moves through these statuses: `received → (needs_info) → awaiting_tier → calling → (call_retry_wait) → tier_confirmed → (duplicate_hold / rl_retry_wait) → project_created → (partial) → completed`. Any step can end in `escalated`, which only a human resolves.

For a faster demo, use the shorter delays noted in `.env.example` (call retries `[60,60]`, clarification reminder 120s, all weekdays).

## Tests

```bash
uv run pytest            # offline suite: all integrations faked, no tokens spent
uv run pytest -m live    # also runs the agent prompts against real OpenAI
sh scripts/check.sh      # ruff + format + pyright strict + tests, stops at the first failure
```

442 offline tests cover the four areas the brief asks for:

| Area | Examples |
|---|---|
| Happy path, end to end | `tests/integration/test_end_to_end.py`: email → intake → call → webhooks → two-key tier decision → Rocketlane project from the Enterprise template → personalized Slack channel → `completed`, running every job through its real entry point |
| Validation | `test_intake_validation.py`, `test_intake_flow.py`: missing customer name, missing contact email, invented values not found in the email, non-deal emails, unknown senders |
| Template accuracy | `test_project_is_created_from_the_tiers_template`: Enterprise → 30-day template with a dedicated CSM (round-robin); Growth → 14-day template with the pooled CSM |
| Edge cases | Rocketlane down (`test_rocketlane_down_retries_then_escalates`), existing project (`test_existing_project_holds_and_escalates`), timed-out create adopted not duplicated, AE doesn't answer (`test_no_answer_schedules_retry_inside_window`), ambiguous answer (`test_unclear_answer_is_retried_not_guessed`, `test_keys_disagree_is_unclear`), Slack down, webhooks replayed |

## Project layout

```
app/
  main.py                FastAPI app, scheduler, Gmail poller wiring
  orchestrator/          state machine and one module per step: intake, voice, project,
                         communication, escalation, resolution, sweep (recovery)
  agents/                Intake, Tier Verifier and Comms agents + prompts/*.md
  integrations/          Gmail, Vapi, Rocketlane, Slack adapters (+ fakes for tests)
  services/              validation, grounding, call policy, CSM routing, audit, Slack copy
  api/                   /deals, /admin, /webhooks/vapi
  ports.py               the interface every integration implements
config/                  templates.yaml (tier → template, CSMs), ae_directory.yaml
scripts/                 gmail_auth, setup_vapi_assistant, check_rocketlane, check_slack, check.sh
tests/                   unit/ and integration/
```

## Assumptions

Open questions sent to the NovaCRM side, with the defaults used until they're answered, are in [docs/open-questions.md](docs/open-questions.md). Each default is a config value, not a code branch.
