# Architecture — NovaCRM Onboarding Agents

Canonical engineering reference. The visual version is `architecture.html`; when they disagree, this file wins. Standards: `engineering-standards.md`. Pending decisions: `open-questions.md`.

## 1. Scope

**In scope:** Gmail-triggered intake, a voice call to confirm the tier, a Rocketlane project from the tier's template, a personalized Slack channel, the Rocketlane overdue automation (UI config), guardrails, audit log + traces, tests, and two demo videos.

**Out of scope:**
- Kickoff scheduling.
- The handoff summary (stretch goal).
- Asana migration or backfilling existing customers.
- Moving the customer's data. That's human work *inside* the Data Migration phase; we create, track and gate those tasks.
- Reading Salesforce.
- Real Slack Connect (mocked).

## 2. Design decisions (pain point → fix)

| Pain point | Root cause | Decision |
|---|---|---|
| Template mix-ups | Tier not in the email; template picked by hand | Tier confirmed on the call with a read-back plus a transcript check; template picked in code from `templates.yaml` |
| Data marked migrated but not verified | "Done" is one click | Customer-approval task gates the Data Migration phase (pending Q6) |
| Escalations fall through | Depends on memory | Rocketlane automation: overdue 1d → PM, 4d → Owner |
| Manual project creation | Unstructured email handoff | Gmail watcher + Intake Agent with strict validation |
| Manual Slack setup | Repetitive, inconsistent | Communication Agent writes the copy, code creates the channel |
| Dedicated vs pooled CSM | Rules live in people's heads | Routing config per tier (pending Q2) |

## 3. Principles

1. Deterministic state machine as the spine; LLMs only for judgment (parse, verify, write).
2. Agents propose (typed output); code validates and executes. Agents have no side-effect tools.
3. Ports & adapters: real adapters for the demo, fakes for tests.
4. Idempotent by default (Gmail message id as the natural key; marker check before retrying a create).
5. Fail to a human, never to a guess.
6. The voice call is async (webhook), so the workflow is split around it with state in the DB.

## 4. System context

```mermaid
flowchart LR
  AE([AE]) -- deal email --> GM[(Gmail CS inbox)]
  GM --> W[Gmail poller] --> O{{Orchestrator}}
  O --> IA[Intake Agent]
  O -- start call --> V[Vapi + Twilio] -- call --> AE
  V -- webhooks --> WH["POST /webhooks/vapi"] --> O
  O --> TV[Tier Verifier]
  O -- create from template --> RL[Rocketlane]
  O --> CA[Comms Agent]
  O -- channel/topic/message --> SL[Slack]
  O --> ESC[Escalation] -- email + Slack --> CSM([CS Manager])
  O --- DB[(SQLite)]
```

## 5. Deal state machine

```
RECEIVED ──► IGNORED                (not a deal / sender not allowed)
RECEIVED ──► NEEDS_INFO ──► RECEIVED (AE replies)  | ──► ESCALATED (48h)
RECEIVED ──► AWAITING_TIER ──► CALLING
CALLING  ──► TIER_CONFIRMED | CALL_RETRY_WAIT ──► CALLING | ESCALATED
TIER_CONFIRMED ──► DUPLICATE_HOLD ──► ESCALATED
TIER_CONFIRMED ──► RL_RETRY_WAIT ──► TIER_CONFIRMED | ESCALATED
TIER_CONFIRMED ──► PROJECT_CREATED ──► COMPLETED | PARTIAL ──► ESCALATED
ESCALATED ──► (human resolve) ──► RECEIVED / AWAITING_TIER / TIER_CONFIRMED / IGNORED (dismiss)
```

Transitions only through `transition()`, which checks `ALLOWED`, uses optimistic locking (`version`), and writes the audit row in the same transaction.

## 6. Components

### Inbox (Gmail)
- `GmailMailbox` (`app/integrations/gmail.py`) polls with `in:inbox -from:me -label:novacrm-processed newer_than:7d` and processes oldest first.
- **Auth:** the OAuth refresh token is refreshed before any call once it expires, and every new token is persisted atomically. A revoked or expired token raises `GmailAuthError`. The poller then stops, `/health` turns 503, and it alerts once. The fix is to re-run `scripts/gmail_auth.py` and restart.
- **Threading:** the Google client is sync on httplib2, which isn't thread-safe, so calls run in a worker thread behind one lock.
- **`handle_inbound`** (`app/orchestrator/workflow.py`):

| Inbound email | Result |
|---|---|
| New message from an allowed sender | Deal created in `RECEIVED` |
| Sender not on the allowlist | Deal recorded as `IGNORED` (audited) |
| Same message seen again | `DUPLICATE`: nothing written |
| Message on a known thread | `REPLY`: audited; a `NEEDS_INFO` deal goes back to `RECEIVED` |

- **Allowlist:** exact address or exact domain, with no subdomain matching. It fails closed: polling refuses to start if both lists are empty.
- **`InboxPoller`** (`app/orchestrator/poller.py`):
  - A list failure backs off (interval × 2^n, capped at 5 min).
  - An unusable message is audited and labelled.
  - A message that keeps failing is abandoned (audited and labelled) after 3 attempts, so it can't block the inbox.
  - The label is only an optimisation. The unique Gmail message id is the real idempotency guard.

### Intake & Routing Agent
1. Sender allowlist (code; applied in `handle_inbound`, see above).
2. LLM parse into `DealIntake` (with an `evidence` quote per field; `null` if absent, never inferred).
3. Grounding check: each evidence quote must appear verbatim in the email, otherwise the field counts as missing.
4. Validation: required fields, email syntax, Salesforce opportunity URL pattern.
5. Enrichment: AE phone from `ae_directory.yaml`.
6. Missing data → reply on the thread, `NEEDS_INFO`. A reply re-parses the whole thread.
7. Routing after the tier is confirmed: `route(tier) → template_id, duration_days, csm, owner` (pure, from config).

Required fields: `customer_company`, `contact_name`, `contact_email`, `ae_name`, `ae_email`, `ae_phone` (directory), `sf_opportunity_url`. Optional: `close_date`, `deal_value`, `notes`.

### Voice call (Vapi)
- **Assistant setup:** `scripts/setup_vapi_assistant.py` saves the assistant in Vapi and imports the Twilio number. It's idempotent and prints `VAPI_ASSISTANT_ID` / `VAPI_PHONE_NUMBER_ID`.
  - The assistant must be *saved*: Vapi attaches our `server.headers` (the `x-vapi-secret` shared secret) only to server URLs that come from a saved resource.
  - Prompt: `app/agents/prompts/vapi_assistant.md`.
  - Tools: `record_plan_tier(tier: enterprise|growth|unclear, ae_confirmed, ae_quote)`, plus `endCall`.
- **Script:** confirm identity → ask for the tier → re-ask once if unclear → read back → explicit yes → record → end.
- **Flow (`app/orchestrator/voice.py`):** `start_call_attempt` → `CALLING` → webhooks → durable job `process_call_result`.
  - Webhooks only store and enqueue. Any LLM work runs in the job, so a slow model never times out a webhook and a crash is retried.
  - The attempt row is committed **before** dialling, so the webhook can find it and a crash can't lose the attempt.
  - `POST /call` is never blindly retried (it could ring the AE twice). A failed or ambiguous start counts as an attempt.
  - Webhooks match the attempt by call id, or by the `deal_id`/`attempt_no` metadata we send if the webhook beats the call id.
- **Two-key confirmation** (pure, `services/call_policy.py`). All of these must hold, otherwise the result is UNCLEAR:
  - The tool call says the AE confirmed tier T.
  - The Tier Verifier agent independently reads T from the transcript.
  - The verifier sees an explicit confirmation.
  - Its verbatim quote is found in the transcript.
- **Outcomes:**

| Outcome | When | Result |
|---|---|---|
| CONFIRMED | Both keys agree | `TIER_CONFIRMED` |
| NO_ANSWER | Did not answer, busy, voicemail | Retry; no LLM spend |
| UNCLEAR | Answered, but no confirmed tier | Retry |
| CALL_ERROR | Provider or start failure | Retry |
| Verifier unavailable | The AE answered but the result can't be checked | `ESCALATED` at once (a human checks the recording; re-calling wouldn't help) |
| Out of attempts | `CALL_MAX_ATTEMPTS` reached | `ESCALATED` |

- **Calling window (G11):** retries follow `CALL_RETRY_DELAYS_SECONDS`, then move into the window (`CALL_WINDOW_*`, `CALL_DAYS`, `CALL_TIMEZONE`). A start job that fires outside the window re-queues itself for the next opening.
- **Endpoints:**
  - `POST /webhooks/vapi` checks the secret (constant-time) **before** reading the body. It rejects unset secrets (fail closed) and caps the body at 2 MB.
  - `end-of-call-report` is deduped on `(call_id, type)` in `webhook_events`.
  - `POST /admin/test-deals` (admin token, disabled in prod) creates a deal in `AWAITING_TIER` and queues a call, so P4 can be tested before intake exists.
- **Not done yet:**
  - Voicemail detection is left at Vapi's default (off). A voicemail pickup with no tool call is treated as UNCLEAR and retried.
  - Q4 (also confirm the customer name?) is a prompt change only.

### Rocketlane
- ⚠️ Verify against the API docs with a real key before building on it. Working assumption: base `https://api.rocketlane.com/api/1.0`, header `api-key`, `POST /projects` accepts a template source. Fallback: create the project, then phases and tasks from config.
- Two templates (Enterprise 30d, Growth 14d), 4 phases, 15 tasks (pending Q3). Proposed task list:
  - Kickoff: review handoff, kickoff call, success criteria & plan.
  - Data Migration: collect export, field mapping sign-off, sandbox import, **customer verifies migrated data**, production import.
  - Configuration: workspace & users, pipelines & fields, integrations, admin training.
  - Go-Live: UAT sign-off, go-live, handoff to support.
- The project carries an `Intake deal id` field as the idempotency marker.
- Duplicate check: our marker first, then the normalized customer name among active projects → `DUPLICATE_HOLD`.
- The overdue automation is configured in the UI: overdue 1d → Project Manager; overdue 4d → Project Owner.

### Communication Agent + Slack
- The agent returns `SlackChannelPlan(channel_name, topic, welcome_message)`. The tone is tier-specific: Enterprise introduces the dedicated CSM with a 30-day timeline; Growth introduces the onboarding pod with a 14-day fast track.
- Code validates the plan: name matches `^[a-z0-9][a-z0-9-_]{0,79}$` and contains the customer slug; the message mentions the customer, tier and CSM; ≤ 3000 chars. One regenerate, then fall back to a fixed template.
- Calls: `conversations.create` → `setTopic` → `chat.postMessage` → invite the internal CSM → `inviteShared` **mocked** (logged and audited).

### Escalation
- **`escalate(session, deal, actor, reason)`** (`app/orchestrator/escalation.py`): moves the deal to `ESCALATED` in the caller's transaction. `transition()` stamps `escalated_at`.
- **Notice job:** the caller then queues `escalation_notice_job` **after commit**.
  - It posts to `#cs-escalations` and emails `ESCALATION_EMAIL` from the CS inbox. The channels are independent, and one success is enough.
  - If every channel fails, it retries after `ESCALATION_NOTICE_RETRY_DELAYS_SECONDS`, then gives up with an ERROR log and an audit row.
  - It's idempotent per escalation (`escalation_notified_at >= escalated_at`) and skips deals a human already resolved.
- **The notice** (pure, `services/escalation_notice.py`) covers:
  - what happened (the escalation rationale);
  - what was tried (each call with its outcome, reason and recording link);
  - the deal details, with the phone masked;
  - how to resolve: `POST /deals/{id}/resolve` (P10), where actor and reason are required and audited.
  - It never includes email bodies or transcripts.
- **System alerts:** `send_system_alert` is Slack-only. The case that triggers it, a revoked Gmail token, is exactly when email can't be used. The inbox poller sends one alert, with the fix, when it stops.
- **Slack adapter** (`app/integrations/slack.py`): the Web API through `HttpService`. slack_sdk's async client would need aiohttp.
  - Slack's `ok:false` codes are mapped to domain errors inside the retry loop: auth errors → `AuthorizationError`; transient errors → retried, or outcome-unknown for posts and creates.
  - Slack Connect is behind `SLACK_CONNECT_ENABLED` (it's a paid feature). When off, the invite is logged.
- **Startup:** if neither Slack nor Gmail + `ESCALATION_EMAIL` is configured, startup logs `escalations_unreachable` at ERROR.

## 7. Retry policy

| Operation | Retry on | In-request | Scheduled | Exhausted |
|---|---|---|---|---|
| Gmail poll | network/5xx/429/rate-limit 403 | 3× exp | tick backoff to 5m | `/health` degraded after 3 failed ticks; auth failure → stop + alert |
| Gmail send | 429/rate-limit 403 (5xx/timeout ambiguous → not retried) | 3× exp | — | logged; Slack still notified |
| LLM call | 5xx/429/timeout | SDK + 2 | — | intake → ESCALATED; comms → fixed template |
| LLM invalid output | schema/grounding fail | 1 re-ask with error | — | NEEDS_INFO |
| Vapi start call | connect error / 429 only (5xx/timeout ambiguous → not redialled) | 3× exp | counts as an attempt | — |
| Voice attempt | not CONFIRMED | 1 re-ask in call | T+0, +10m, +30m (demo 1m), moved into the calling window | ESCALATED (verifier down: at once) |
| Rocketlane | 5xx/429/timeout | 3× exp (1/2/4s) | +1m, +5m, +15m | ESCALATED (4xx: immediately) |
| Slack | 5xx/429 | 3× exp | +1m, +5m | PARTIAL → ESCALATED |
| AE clarification | no reply | — | reminder +24h | ESCALATED +48h |
| Escalation notice | every channel failed | adapter retries | +5m, +15m | ERROR log + audit (gave up) |

Timeouts are not failures: search for our marker before retrying any create. Webhooks dedupe on `(call_id, type)`. Jobs persist in SQLite; a startup sweep re-queues stranded deals.

**How this is enforced (`app/integrations/http.py`):** every REST adapter goes through `HttpService.request`. It sets connect and read timeouts, caps in-flight requests per service, and maps responses to domain errors:
- 408/425/429/5xx → `RetryableError` (honours `Retry-After`, capped at the max backoff).
- Other 4xx → `PermanentError`.

Creates pass `idempotent=False`:
- They are retried in-request only when the request provably never arrived (a connect error) or was refused (429).
- A read timeout, dropped connection or 5xx raises `OutcomeUnknownError`, and the orchestrator checks for its marker before scheduling a retry.

**Ports (`app/ports.py`):** `Mailbox`, `VoiceCaller`, `ProjectStore`, `ChatWorkspace`. `ProjectStore.list_projects()` returns every project, and duplicate and marker detection run on that in code. Rocketlane's server-side filter for customer names is unverified, and about 200 projects fit comfortably in a paginated list. Fakes with scripted faults are in `app/integrations/fakes.py`.

## 8. Guardrails

| # | Guardrail | Source |
|---|---|---|
| G1 | Required fields, else ask the AE | Spec |
| G2 | No project without a confirmed tier (enforced by the state machine) | Spec |
| G3 | Audit log: timestamp, input, output, rationale | Spec |
| G4 | Grounding: extracted fields must quote the email | Ours |
| G5 | Two-key tier confirmation | Ours |
| G6 | Sender allowlist | Ours |
| G7 | Idempotency + duplicate-project hold | Ours |
| G8 | Agents have no side-effect tools; email treated as untrusted | Ours |
| G9 | Bounded retries with backoff | Ours |
| G10 | Slack output validation | Ours |
| G11 | Business-hours calls + identity check | Ours |
| G12 | Data-migration verification gate | Pending Q6 |
| G13 | Webhook + admin auth, secrets from env | Ours |

## 9. Data model

- `deals`: id, gmail_message_id (unique), gmail_thread_id, status, version, customer/AE fields, plan_tier, tier_source (voice | human_override), rocketlane_project_id, slack_channel_id, timestamps.
- `call_attempts`: deal_id, attempt_no, vapi_call_id, ended_reason, tool_tier, verifier_tier, outcome, transcript, recording_url, times.
- `audit_log`: ts, deal_id, actor, action, input_json, output_json, rationale, status, latency_ms, trace_id. Append-only.
- `agent_traces` / `agent_spans`: local copy of Agents SDK traces (group_id = deal id).
- `webhook_events`: (call_id, type) unique, for dedupe.
- `routing_state`: round-robin cursor per tier.
- APScheduler job store tables.

## 10. API

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/health` | none | Database and Gmail inbox status (503 when degraded) |
| POST | `/webhooks/vapi` | shared secret | tool-calls, status-update, end-of-call-report |
| GET | `/deals?status=&limit=` | admin token | List deals, newest first |
| GET | `/deals/{id}` | admin token | Deal state, with call attempts (transcripts, recordings) |
| GET | `/deals/{id}/audit` | admin token | Decision timeline |
| GET | `/deals/{id}/traces` | admin token | Agent traces grouped by deal |
| POST | `/deals/{id}/resolve` | admin token | Human resolution of an `ESCALATED` deal (actor and reason required) |
| POST | `/deals/{id}/retry` | admin token | Re-queue the current step through `advance()` (idempotent, audited) |
| POST | `/admin/poll` | admin token | Poll the CS inbox now (demos) |
| POST | `/admin/test-deals` | admin token | Create a deal in `AWAITING_TIER` and queue a call (disabled in prod) |

**Resolve actions** (`app/orchestrator/resolution.py`):

| Action | Deal becomes | Then |
|---|---|---|
| `confirm_tier` (+ `plan_tier`) | `TIER_CONFIRMED` | Recorded as `tier_source=human_override` |
| `retry_call` | `AWAITING_TIER` | Exactly one more call |
| `reprocess_intake` | `RECEIVED` | Intake re-reads the thread |
| `dismiss` | `IGNORED` | Closed |

Errors: 404 for an unknown deal; 409 if the deal isn't escalated or changed concurrently; 422 for an invalid body. The audit actor is `human:<actor>`.

No custom UI. The demo uses Gmail, the phone, Rocketlane, Slack, terminal logs, FastAPI `/docs` and OpenAI Traces.

### Recovery and shutdown
- **`advance(rt, deal)`** (`app/orchestrator/progress.py`) queues whatever the deal's current state needs next. It's shared by resolve, retry and the sweep. It only queues and never runs a step inline.
- **Recovery sweep** (`app/orchestrator/sweep.py`): runs at startup and every `SWEEP_INTERVAL_SECONDS`. For each non-terminal deal:
  - a waiting call with no job → queued;
  - an owed escalation notice → queued;
  - an end-of-call report stored but not processed → processing queued;
  - a `CALLING` deal with no report `CALL_REPORT_GRACE_SECONDS` after the max call duration → the attempt is closed as `CALL_ERROR`, and normal retry or escalation follows;
  - statuses owned by unbuilt phases → only counted.
  - Every recovery is audited as `sweeper`.
- **Graceful shutdown:** jobs are `tracked`. Shutdown pauses the scheduler, drains running jobs for up to `SHUTDOWN_GRACE_SECONDS`, then closes adapters and the database. (Closing the DB under a running job used to hang the event loop.)

## 11. Observability

- Audit log answers **which step failed and why**. Traces answer **what the agent saw and returned**.
- Traces go to OpenAI Traces and the local `SQLiteTraceProcessor`. One trace per workflow step (`intake`, `voice_call`, `project_creation`, `communication`, `escalation`), all with `group_id = deal_id`. Non-LLM steps go in `custom_span`s.
- Debug order: `/deals/{id}` → `/audit` → `/traces` → Vapi dashboard (recording).

## 12. Testing (Part 4 mapping)

| Area | Tests |
|---|---|
| Happy path | `test_enterprise_deal_end_to_end` |
| Validation | missing name / contact email / bad email / malformed / ungrounded field |
| Template accuracy | `test_tier_routing[enterprise\|growth]` |
| Edge cases | Rocketlane 503 → escalate; duplicate → hold; no answer → retries → escalate; unclear / verifier mismatch / unknown plan |
| Extra | idempotent reprocess, prompt injection, human override audited, token refresh failure, webhook auth, concurrent transition |

## 13. Demo vs production

| Area | Demo | Production |
|---|---|---|
| Trigger | Gmail polling | Gmail watch → Pub/Sub, with polling as a backstop |
| Tier source | Voice call | Salesforce primary, call to confirm |
| Slack | Internal channel, mocked Slack Connect | Real Slack Connect |
| Storage/jobs | SQLite + APScheduler | Postgres + Alembic, durable queue / Temporal |
| Hosting | Laptop + ngrok | Cloud Run/ECS, secrets manager |
| Auth | Shared secrets | SSO, signed webhooks, least-privilege keys |
| Existing customers | Not imported | Idempotent backfill script with dry-run |
| Observability | Audit + traces + console | Dashboards, stuck-state alerts, PII redaction, retention |

## 14. Implementation phases

| Phase | Work | Depends on |
|---|---|---|
| P0 | Accounts & spikes (user): OpenAI, Gmail + GCP OAuth, Twilio, Vapi, ngrok, Slack, Rocketlane | — |
| P1 | Skeleton: config, models, db, state machine, audit, logging, tracing, main | — |
| P2 | Ports + fakes + conftest | P1 |
| P3 | Gmail connector + poller + `handle_inbound` | P2 |
| P4 | Vapi assistant script, `start_call`, webhook, tier verifier, retries | P2 |
| P5 | Notifier + escalation | P3 |
| P6 | Intake agent + validation + clarification loop | Q1 |
| P7 | Rocketlane templates, adapter, routing, dedupe | access, Q2, Q3 |
| P8 | Comms agent + Slack | P7 |
| P9 | Rocketlane automation (UI) | access |
| P10 | Admin API (deal views, resolve, retry, poll) + recovery sweep + graceful shutdown | P5 |
| P11 | Full test suite | all |
| P12 | README, Part 1 doc, videos | all |

Each phase has a "done when" check. See `architecture.html#impl` for the detailed file and function list.
