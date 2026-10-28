# Engineering Standards

Code quality comes first, functionality second. Every rule here exists to close a specific hole. If a rule has to be broken, write down why in the code and in the PR or commit message.

## 1. Code quality

- **Typing:** full type hints on all functions; `pyright` in strict mode for `app/`. No `Any` without a comment explaining why.
- **Lint/format:** `ruff check` + `ruff format`. No unused imports, no wildcard imports, and no `print` (use the logger).
- **Small units:** functions do one thing. Orchestrator steps are short and read like the state diagram.
- **Errors:** no bare `except:` and no `except Exception: pass`. Catch specific exceptions, convert them to domain errors (`IntegrationError`, `RetryableError`, `PermanentError`, `ValidationFailed`), and handle them in the orchestrator.
- **No magic values:** timeouts, retry counts, delays, windows and model names come from `Settings`.
- **Context managers:** annotate with `Generator` / `AsyncGenerator` (pyright flags `Iterator` as deprecated).
- **Pure core:** validation, routing, tier decision and state transitions are pure functions with no I/O, so they are trivially testable.
- **Naming:** domain language from the brief (deal, AE, tier, CSM, phase, escalation). Use it consistently in code, logs and docs.
- **Comments:** explain *why*, not *what*. Docstrings on public functions of adapters and services.

## 2. Authentication & secrets

- **Gmail OAuth:** use `google.oauth2.credentials.Credentials` with a refresh token. Before each API batch, `if creds.expired: creds.refresh(Request())`, then **persist the refreshed token atomically** (write to temp file, then `os.replace`). On `RefreshError` (revoked, or the 7-day expiry in Testing mode): stop polling, set `/health` to degraded with a clear reason, log at ERROR, and alert once (not every tick).
- **Vapi / Rocketlane / Slack / OpenAI:** static API keys from env via `SecretStr`. Never log them; `repr` must stay masked.
- **Webhooks:** `/webhooks/vapi` verifies the shared secret header with `hmac.compare_digest`. Reject with 401 before parsing the body.
- **Admin endpoints:** protected by an `X-Admin-Token` header (constant-time compare). In prod: SSO.
- **Secrets on disk:** `secrets/` and `.env` are git-ignored. `.env.example` lists every variable with no real values.
- **Least privilege:** Gmail scope `gmail.modify` only (read + label + send). Slack bot scopes limited to what the build uses.

## 3. Reliability

- **Timeouts everywhere:** every httpx client has explicit connect/read timeouts. No call may hang forever.
- **Bounded retries:** tenacity with exponential backoff + jitter, only on retryable errors (network, 5xx, 429 honouring `Retry-After`). 4xx is permanent. Then scheduled retries. Then a named failure state + escalation. See the retry table in `architecture.md`.
- **Idempotency:**
  - Deal natural key = Gmail message id (unique constraint).
  - Before retrying a create (Rocketlane project, Slack channel), look for our own marker first, because a timeout may have succeeded.
  - Webhook handlers dedupe on `(call_id, message_type)`.
- **Concurrency:**
  - The Gmail poll job runs with `max_instances=1, coalesce=True`.
  - Deal rows carry a `version` column (optimistic locking). `transition()` does `UPDATE ... WHERE id=? AND version=?`. On a conflict, reload and re-evaluate. This prevents a webhook and a retry job racing on the same deal.
  - One DB session per unit of work. A state transition and its audit row commit in the same transaction.
- **Durability:** scheduled jobs live in the SQLAlchemy job store. A startup sweep re-queues deals in non-terminal states with no pending job.
- **Graceful shutdown:** the lifespan shuts down the scheduler (`wait=True`) and closes the httpx clients.
- **Health:** `/health` reports Gmail token state, DB, and last successful poll time.

## 4. Security

- Email bodies and transcripts are untrusted. Wrap them in delimiters in prompts, and give agents no side-effect tools.
- Validate all inbound data with Pydantic at the boundary (webhooks, API bodies, LLM outputs, YAML config).
- Sender allowlist for inbound email.
- Slack output validated (channel-name regex, length, required mentions) before sending.
- PII: transcripts and emails stay local. Logs at INFO contain ids and field names, not full bodies. `trace_include_sensitive_data` is True only in dev.

## 5. Scale (design for it, don't over-build it)

Current load is ~200 customers and a handful of deals a week, so a single process with SQLite is correct. The code must make the next step a config or adapter change, not a rewrite:

- All state is in the DB, never in process memory. The app is stateless apart from the scheduler.
- DB access goes through SQLModel/SQLAlchemy only, so moving to Postgres means changing the URL plus adding Alembic.
- Ports isolate transports: Gmail polling → Pub/Sub push, and APScheduler → a durable queue (Arq, Celery or Temporal), each without touching business logic.
- Rate limits: respect provider 429s, and add a per-integration concurrency limit (`asyncio.Semaphore`) so a burst of deals can't stampede Rocketlane or Vapi.
- Pagination handled in every list call (Gmail, Rocketlane search).

## 6. Observability

- structlog JSON in prod, pretty console in dev. Every log line is bound with `deal_id`, and `call_id` where relevant.
- `audit_log` row for every decision: timestamp, actor, action, input, output, rationale, status, latency, trace id.
- Agents SDK tracing to OpenAI + `SQLiteTraceProcessor` for a local copy. Every trace uses `group_id=deal_id`.
- Non-LLM steps are wrapped in `custom_span` so a trace shows the whole story.

## 7. Testing

- Every behaviour has a test. Every bug fix adds a regression test first.
- Unit tests for the pure core. Integration tests run the orchestrator with fakes and respx-mocked HTTP.
- No network in the default run. Real-LLM tests are marked `@pytest.mark.live`.
- `freezegun` for retry timing, business hours and the 24h/48h clarification windows.
- Required coverage: the Part 4 scenarios (happy path, validation, template accuracy, Rocketlane down, duplicate project, no answer, ambiguous tier) plus idempotency, prompt injection, token refresh failure, webhook auth failure, and concurrent transition conflict.

## 8. Definition of done

A change is done only when all of these hold:
- [ ] `ruff check`, `ruff format --check` and `pyright` are clean
- [ ] `pytest` is green, and new behaviour has tests
- [ ] The failure path is handled and tested, not just the happy path
- [ ] It is logged and audited, and secrets don't appear in logs
- [ ] Config is in `Settings` / YAML, not hard-coded
- [ ] `docs/architecture.md` is updated if the design changed
