# Customer Onboarding Automation: Requirements Analysis

| | |
|---|---|
| **Client** | NovaCRM, Customer Success |
| **Prepared for** | Rocketlane: Forward Deployed Engineer assessment, Part 1 |
| **Prepared by** | Karthick Sriram · 30 October 2026 · v1.0 |
| **Status** | Submitted. Open questions answered by Karthik Ramaiah (Rocketlane, acting for NovaCRM) on 29 Oct 2026; final acceptance pending client UAT (§12) |

## 1. Executive summary

**Problem.** NovaCRM's Customer Success team onboards every customer by hand, starting from a free-text email the Account Executive (AE) sends when a deal closes. Three failures recur: the **wrong template** is applied because the email doesn't state the plan tier; **data migration is marked done** without the customer verifying it; and **stalled work isn't escalated**.

**Proposed solution.** An agentic workflow that turns a deal email into a running onboarding project. An Intake Agent extracts and validates the deal details and asks the AE for anything missing; a voice agent phones the AE to confirm the tier; the system creates the Rocketlane project from the matching template and assigns the CSM; a Communication Agent opens a personalised Slack channel. Rocketlane automations escalate overdue tasks and request the customer's migration sign-off. Deterministic code controls every step; language models only read and write text. When anything is missing or uncertain, the system asks a person instead of guessing.

**What was built.** A working system, verified against live Gmail, a real phone call, Rocketlane and Slack, with 450 automated tests (§10).

**Main gaps before production** (§11–§12): blocked-task escalation (Priya's original rule) is not yet covered; the migration approval does not yet *block* the phase from closing; and the human escalation console is an API, not a business-user interface.

## 2. Context, objectives and success measures

NovaCRM (~200 customers, a few new deals a week) runs onboarding in Asana with ~15 tasks across four phases (Kickoff, Data Migration, Configuration, Go-Live). Enterprise customers get a dedicated CSM and a 30-day plan; Growth customers get a pooled CSM and 14 days.

| ID | Objective | Measure (proposed target, to be agreed with the client) |
|---|---|---|
| BO&#8209;1 | No template errors | Template matches the AE-confirmed tier on 100% of projects; 0 projects created on an unconfirmed tier |
| BO&#8209;2 | Migrated data is verified by the customer | Every migration has a recorded customer approval; migration-related customer escalations trend to 0 |
| BO&#8209;3 | Slipping work is escalated automatically | 100% of tasks overdue ≥ 1 day notify the Project Manager; ≥ 4 days notify the Project Owner |
| BO&#8209;4 | No manual set-up | Deal email → project and channel ready in under 1 business hour, excluding AE response time; share of deals needing a person tracked monthly |

**How each measure is defined**

- **BO‑1.** A template error is a project whose Rocketlane template doesn't match the deal's confirmed tier. A tier counts as confirmed only when the AE confirmed it on the call and the transcript check agreed, or the CS Manager recorded it when resolving an escalation. Every project is compared against its deal record each month.
- **BO‑2.** A migration counts as verified when the customer approved the "Customer verifies migrated data" task in Rocketlane before the Data Migration phase was closed. A migration-related escalation is any customer complaint the CS team logs about missing or incorrect migrated data.
- **BO‑3.** A task is overdue once its due date has passed and it isn't complete. Each month, the overdue tasks are checked to confirm the Project Manager was notified at 1 day overdue, and the Project Owner at 4 days.
- **BO‑4.** The clock starts when the deal email arrives in the CS inbox and stops when setup is complete. Setup is complete when the project exists from the right template with the CSM assigned, and the Slack channel is open with its topic, welcome message and customer invite. Only business hours count (Mon–Fri, 09:00–19:00 IST). Time spent waiting for the AE to reply to a clarification email or to answer the call is excluded. A deal "needs a person" if it was escalated at least once. The share is calculated over deal emails, not ignored non-deal mail.

**Non-goals** (reasonable goals deliberately excluded from this phase): kickoff scheduling, the end-of-onboarding handoff summary, Salesforce integration, migrating existing Asana projects (§13).

## 3. Stakeholders

| Stakeholder | Role | Needs from the new process |
|---|---|---|
| **CS Manager** (Priya) | Owns onboarding quality; Project Owner; resolves escalations | Correct templates; escalations that don't rely on memory; a record of every decision; a named backup when unavailable |
| **CSM** | Runs each onboarding; Project Manager | No manual set-up; early warning on slipping tasks; a way to reassign work |
| **Account Executive** | Hands the deal over | One email and one short call; asked only for what is genuinely missing |
| **Customer** | Completes customer-side tasks, verifies data | A shared channel on day one; a clear sign-off step for their data |
| **Sales leadership / RevOps** | Owns the AE handoff, Salesforce, the AE directory | Minimal AE disruption; ownership of the AE contact list |
| **IT / Security** | Approves access and data handling | Least-privilege access; call-recording and AI-data policies (§9) |
| **Rocketlane & Slack admins** | Maintain templates, automations, the Slack workspace | Clear configuration ownership after hand-over |

## 4. Current workflow and root causes

![Figure 1. Today's onboarding process, its failure points, and the proposed step that removes each](images/before-after.svg)

| # | Step today | Owner | Tool | Problem observed |
|---|---|---|---|---|
| 1 | Email CS about the closed deal | AE | Email | Free text; plan tier never included; fields sometimes missing |
| 2 | Work out the tier and pick a template | CSM | Judgement | Enterprise and Growth templates get mixed up |
| 3 | Create the project; work phase checklists | CSM | Asana | Migration tasks ticked without the customer verifying data |
| 4 | Create the shared Slack channel; schedule kickoff | CSM | Slack, calendar | Repetitive and inconsistent |
| 5 | Escalate tasks blocked > 3 days | CSM | Memory | "Falls through the cracks" |
| 6 | Write handoff summary; move to support | CSM | Document | Manual write-up |

| Problem | Root cause |
|---|---|
| Wrong template | The tier isn't in the handoff, so a person infers it; nothing checks the choice |
| Unverified migration | Completion is one click; there is no customer confirmation step |
| Missed escalations | No system watches task status or due dates |
| Slow, inconsistent set-up | Data is copied by hand from an unstructured email into several tools |

## 5. Requirements

Priority uses MoSCoW (**M**ust / **S**hould / **C**ould / **W**on't this phase). Each requirement is verified by the test or evidence listed in §10.

### 5.1 Functional requirements

| ID | P | The system shall… | Acceptance criterion | Status in this build |
|---|---|---|---|---|
| FR&#8209;1 | M | Monitor the CS Gmail inbox and start one workflow per deal email from an approved AE | Approved sender → exactly one workflow; other senders and non-deal emails logged and ignored | Met |
| FR&#8209;2 | M | Extract customer name, contact name and email, AE name and Salesforce link | Every value appears verbatim in the AE's email; nothing inferred | Met |
| FR&#8209;3 | M | Validate required fields before proceeding | Missing or malformed field (invalid email, non-Salesforce link) stops the workflow | Met |
| FR&#8209;4 | M | Ask the AE for missing information instead of guessing | Reply on the thread lists exactly what's needed; reminder after 24 h; escalation after 48 h | Met |
| FR&#8209;5 | M | Phone the AE with a voice AI agent to confirm Enterprise or Growth | AE called only on the number in the AE directory, never one from an email; an AE not in the directory is escalated to a person; tier asked, read back with the customer name, explicit "yes" required | Met |
| FR&#8209;6 | M | Create a project only on a clear, unambiguous confirmation | Unclear, missing or contradictory answer never creates a project; no default tier exists | Met |
| FR&#8209;7 | M | Retry or escalate when the call fails or is unclear | Retried within business hours, max 3 attempts; then escalated to a person | Met |
| FR&#8209;8 | M | Create the Rocketlane project via API from the tier's template and timeline | Enterprise → 30-day template, dedicated CSM; Growth → 14-day template, pooled CSM | Met |
| FR&#8209;9 | M | Prevent duplicate onboarding projects | Active project for the same customer → no second project; deal held for a person | Met |
| FR&#8209;10 | M | Create a Slack channel shared with the customer, personalised by tier | Name, topic and welcome name the customer, tier and CSM; customer invited via Slack Connect | Met |
| FR&#8209;11 | M | Escalate overdue tasks in Rocketlane | Overdue 1 day → Project Manager; 4 days → Project Owner | Met (configured in Rocketlane) |
| FR&#8209;12 | M | Obtain the customer's sign-off on migrated data | Customer approval is requested on the verification task, and the Data Migration phase can't close until it is given | **Partially met: production gap.** Approval is requested, but it doesn't yet block the phase from closing (§12) |
| FR&#8209;13 | M | Log every agent action | Timestamp, actor, input, output and rationale recorded per step | Met |
| FR&#8209;14 | M | Let a person take over any stalled deal | Escalation reaches the CS Manager by email and Slack with context; they can confirm the tier, retry, re-read the email or close the deal; their decision is recorded | Met (through the operations API; business-user console is a production gap, §11) |
| FR&#8209;15 | S | Escalate tasks *blocked* for more than 3 business days | A task blocked for more than 3 business days notifies the Project Manager | **Not met: production gap.** Deferred because the client confirmed overdue-only for this phase (Q5); planned as a Rocketlane status automation (§12–§13) |

P is the priority: M = Must, S = Should. A requirement's priority says whether it is mandatory; its status says whether this build meets it. **FR-12 and FR-15 are not fully met.** They are open production gaps, not delivered requirements.

### 5.2 Non-functional requirements

| ID | Area | Requirement |
|---|---|---|
| NFR&#8209;1 | Reliability | No step fails silently: every external call has a time limit, bounded retries and a defined outcome (success or escalation) |
| NFR&#8209;2 | Integrity | Retries and restarts never create a second call, project, channel or message for the same deal |
| NFR&#8209;3 | Auditability | Any deal's full history, including the reason for each decision, can be reviewed afterwards |
| NFR&#8209;4 | Security | Only approved senders start a workflow; webhooks authenticated; email and call content treated as untrusted (prompt-injection resistant) |
| NFR&#8209;5 | Calling etiquette | Calls only in business hours, at most 3 per deal |
| NFR&#8209;6 | Changeability | Required fields, templates, CSM pools, retry limits and calling hours change by configuration, not code |
| NFR&#8209;7 | Scale | ~200 customers, a handful of deals per week; production path for higher volume in §11 |

### 5.3 Constraints and dependencies

- The tier must be confirmed **by a real voice call**; projects created **through the Rocketlane API**; overdue escalation must be a **Rocketlane automation**, not an agent (from the brief).
- Dependencies: Gmail API (OAuth), Vapi and Twilio telephony, OpenAI, Rocketlane API and automations, Slack (Slack Connect is a paid feature).

## 6. Clarifications and assumptions

**Questions asked** (28 Oct, answered 29 Oct by Karthik Ramaiah for NovaCRM). Each answer became configuration, not code.

| # | Question | Answer | Resulting decision |
|---|---|---|---|
| Q1 | Does the email contain the contact email and AE phone? | Contact email yes; AE phone from a directory | AE directory is the only phone source; a number in an email is never called; required fields configurable |
| Q2 | How are CSMs chosen; who is PM and Owner? | Round-robin (Enterprise), pooled (Growth) | CSM = Project Manager; CS Manager = Project Owner |
| Q3 | Same tasks for both tiers? | Yes, durations differ | Two templates, same 15 tasks, 30 vs 14 days |
| Q4 | Confirm the customer on the call? Retries? | Yes; 3 attempts | Read-back includes the customer; then escalate to the CS Manager |
| Q5 | Blocked tasks as well as overdue? | Overdue only | FR-15 deferred and listed as a residual gap |
| Q6 | Customer sign-off on migration? | Yes | Rocketlane approval on the verification task |

**Assumptions** (to be validated with the client)

| ID | Assumption | Impact if false |
|---|---|---|
| A1 | Rocketlane replaces Asana for **new** onboardings; active Asana customers are not migrated | Duplicate check can't see customers still onboarding in Asana: needs a cut-over list |
| A2 | Only Enterprise and Growth exist | New tiers need a template and configuration entry |
| A3 | The tier comes from the AE call, not Salesforce | If the tier is reliable in Salesforce, the call becomes a confirmation only (§13) |
| A4 | AEs work 09:00–19:00 IST, Mon–Fri | Per-AE time zones needed for distributed teams |
| A5 | Same company name means same customer | Upsells or re-onboardings could be held wrongly; production should match on Salesforce account ID |
| A6 | RevOps maintains the AE directory and sender allowlist | Stale data → wrong number called or deals ignored |

## 7. Proposed agentic workflow

![Figure 2. Proposed workflow: the happy path, how each step fails safely, and where a person takes over](images/proposed-workflow.svg)

**Decision rights:** who may decide what, and why.

| Actor | Kind | Decides | May not | Why this split |
|---|---|---|---|---|
| Intake Agent | LLM, no tools | What the email says (fields + quotes), whether it's a deal | Create, call or infer the tier | Free text needs language understanding; quotes make every value checkable |
| Voice agent | LLM voice assistant (Vapi) | How to phrase the conversation; records the AE's answer | Suggest a tier or proceed on its own | A real conversation is required; it only captures the answer |
| Tier Verifier | LLM, no tools | Independent reading of the transcript | Override the voice record | A second, independent check catches mis-hearing; disagreement = unclear |
| Workflow engine | Deterministic code | Every action and every business rule | Act on unvalidated data | Rules must be predictable, testable and auditable |
| Communication Agent | LLM, no tools | Wording of the topic and welcome | Post unchecked text | Personalised tone; code checks it names customer, tier and CSM, else a standard message |
| Rocketlane automations | Rocketlane configuration | Overdue alerts, approval requests | n/a | Lives where the CSMs work; no custom code to maintain |
| CS Manager | Person | Anything escalated | n/a | Final authority; every decision logged |

**Alternatives considered:** reading the tier from Salesforce (rejected for this phase; the brief requires a call, and it stays as a future pre-fill); building escalation in code (rejected; Rocketlane automations are maintainable by the client); a single LLM check of the call (rejected; two independent records with a required read-back are stronger against mis-hearing).

## 8. Exceptions and guardrails

| ID | Situation | Required behaviour |
|---|---|---|
| E&#8209;1 | Missing customer name or contact email | Stop; ask the AE for exactly what's missing; remind at 24 h; escalate at 48 h |
| E&#8209;2 | Value not actually in the email | Treated as missing; inferred values are never accepted |
| E&#8209;3 | Unknown sender or non-deal email | Logged and ignored; no call |
| E&#8209;4 | AE doesn't answer | Retry after 10 and 30 min within business hours; escalate after 3 attempts |
| E&#8209;5 | Ambiguous answer ("probably the bigger one") | Asked once more; still unclear → recorded as unclear, retried, then escalated; never mapped to a tier |
| E&#8209;6 | Voice record and transcript disagree | Treated as unclear |
| E&#8209;7 | Rocketlane unavailable | Retry after 1, 5 and 15 min, then escalate; never assume success |
| E&#8209;8 | Request timed out but project was created | Found by its deal reference and adopted; no duplicate |
| E&#8209;9 | Customer already has an active project | Held and escalated; no second project |
| E&#8209;10 | Slack or the copy model unavailable | Resume from the last completed step; standard message replaces generated copy |

## 9. Security, privacy and compliance

| Topic | This build | Required for production |
|---|---|---|
| Call recording and consent | Calls are recorded and transcribed by Vapi | Recording disclosure at the start of the call; consent rules per AE jurisdiction; retention policy |
| Customer data sent to AI | Email text and transcripts go to OpenAI | Enterprise data-processing agreement, zero-retention settings, data-minimised prompts |
| Data retention | Audit log keeps inputs and outputs indefinitely; secrets masked | Retention period, PII redaction in logs, deletion on request |
| Access | Operations API behind a single admin token | SSO with role-based access; read-only audit role |
| Integrations | OAuth for Gmail; API keys in an environment file | Secrets manager, key rotation, least-privilege scopes reviewed by IT |
| Phone source | AE directory only. A number in an email is never called; an AE missing from the directory is escalated to a person | Directory synced from the system of record (HR or Salesforce users) instead of a config file |

## 10. What was built and how it was verified

**Built:** the full workflow in Python (FastAPI, OpenAI Agents SDK), with Gmail polling, Vapi + Twilio voice, the Rocketlane and Slack APIs, a durable job queue and an audit log. Two Rocketlane templates and both automations are configured in the client workspace.

**Live verification** (29 Oct 2026): a deal email read and validated from Gmail; a real phone call in which the AE confirmed "Enterprise" and both checks agreed; a Rocketlane project created from the Enterprise template with a dedicated CSM; the personalised Slack channel with an agent-written welcome; a live escalation email and Slack alert when the intake model failed; a timed-out Rocketlane create adopted without a duplicate. The overdue automation is configured but has not yet fired on a real task.

**Traceability: problem → requirement → guardrail → evidence**

| Problem | Requirements | Guardrails | Automated test (or evidence) |
|---|---|---|---|
| End-to-end handoff | FR-1–FR-10 | — | `test_deal_email_to_onboarding_channel` (email → channel, every job through its real entry point) |
| Missing or wrong data | FR-2, FR-3, FR-4 | E-1, E-2, E-3 | `test_missing_required_field`, `test_hallucinated_value_is_rejected`, `test_gaps_ask_the_ae_on_the_thread`, `test_no_reply_gets_a_reminder_then_escalates`, `test_unknown_sender_is_recorded_as_ignored` |
| Wrong template | FR-5–FR-8 | E-4, E-5, E-6 | `test_project_is_created_from_the_tiers_template` (both tiers), `test_dedicated_csms_rotate_across_deals`, `test_no_answer_schedules_retry_inside_window`, `test_unclear_answer_is_retried_not_guessed`, `test_keys_disagree_is_unclear` |
| Duplicates and outages | FR-9, NFR-1, NFR-2 | E-7, E-8, E-9, E-10 | `test_rocketlane_down_retries_then_escalates`, `test_timed_out_create_is_adopted_not_duplicated`, `test_existing_project_holds_and_escalates`, `test_slack_down_retries_and_resumes_without_duplicates` |
| Inconsistent set-up | FR-10 | E-10 | `test_bad_copy_is_rejected`, `test_fallback_is_tier_specific`, `test_two_bad_drafts_fall_back_to_the_template` |
| Missed escalations / unverified data | FR-11, FR-12 | — | Rocketlane configuration (screenshot in the demo) |
| Auditability and human control | FR-13, FR-14 | — | Audit trail per deal; `test_human_tier_confirmation_queues_the_project` |

## 11. Production readiness

| Area | This build | Change for production | Priority |
|---|---|---|---|
| Customer in the Slack channel | Real Slack Connect invite (`conversations.inviteShared`) to the customer contact | Track invite acceptance and remind if it is still pending | Should |
| Escalation console | API calls with an admin token | Slack buttons or a Rocketlane form for the CS Manager | Must |
| Duplicate detection | Normalised company name | Match on Salesforce account or opportunity ID; name match only as a prompt for review | Must |
| Telephony | Twilio trial: a trial notice plays and only verified numbers can be called | Paid account, local caller ID per AE country | Must |
| Privacy and access | §9 | §9 | Must |
| Email trigger | Polling every 30 s via a single Gmail account | Gmail push (Pub/Sub); shared mailbox with delegated access | Should |
| Hosting and data | One process, SQLite, ngrok for webhooks | Managed hosting, PostgreSQL, real ingress with TLS | Should |
| Operations | Health check, logs, traces | Alerts on escalations and stuck deals, run-book, on-call owner, cost per deal tracked | Should |
| Time and capacity | IST calling hours; CSM round-robin ignores leave and load | Per-AE time zones; capacity-aware assignment with manual override | Could |

## 12. Risks and residual gaps

| Risk / gap | Impact | Mitigation |
|---|---|---|
| **Blocked tasks** (Priya's ">3 days blocked") are not escalated; only overdue tasks are | Stuck-but-not-late work still slips | Rocketlane automation on status *Blocked* (FR-15), first item on the roadmap |
| **Migration approval doesn't gate the phase**: requested when the task completes, but the phase can still close | Unverified data can still reach production import | Make production import depend on an approved verification; CSM checklist until then |
| AE unreachable for days | Onboarding start delayed | Retries in business hours; CS Manager can confirm the tier manually |
| Model misreads email or transcript | Wrong data or tier | Quoted values, two independent tier checks; anything uncertain goes to a person |
| CS Manager is a single point of escalation | Escalations wait during absence | Named backup owner; escalations also posted to a shared channel |
| AE fatigue from calls | Sales pushback | One short call, business hours only; Salesforce pre-fill planned |

## 13. Scope, rollout and roadmap

| In scope | Out of scope this phase (reason) |
|---|---|
| Intake, validation, clarification; voice tier confirmation; Rocketlane project and CSM; personalised Slack channel; overdue escalation and migration approval; audit trail and human resolution; automated tests | Kickoff scheduling (needs calendar access and customer availability); handoff summary to support (needs end-of-project data); blocked-task escalation (Q5); Salesforce (A3); Asana migration (A1) |

**Rollout:** (1) **Pilot:** run alongside the current process for 2–3 weeks on new deals, with the CS Manager reviewing each one. (2) **UAT:** the client runs the edge cases in §8 and signs off against §2's measures. (3) **Cut-over:** new deals go through the system only; CSMs and AEs get a one-page guide. (4) **Ownership:** Rocketlane templates and automations stay with the Rocketlane admin; the AE directory with RevOps; the system with a named engineering owner.

**Roadmap:** blocked-task escalation → migration approval gating the phase → Salesforce tier pre-fill → kickoff scheduling → handoff summary at Go-Live.

## Appendix A. Solution architecture

![Figure 3. Solution architecture](images/architecture.svg)

Technology: Python 3.12, FastAPI, OpenAI Agents SDK, Pydantic, SQLite (PostgreSQL in production), APScheduler, Vapi with Twilio, Rocketlane REST API, Slack Web API.
