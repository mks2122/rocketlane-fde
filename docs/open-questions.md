# Open Questions & Assumptions

Questions sent to Karthik (karthikr@rocketlane.com) on Wed 28 Oct 2026. Every pending answer is a **config value** with a working default, so a late answer means editing config, not code.

This file feeds the Part 1 requirements document: answered questions become "Confirmed with Karthik", and unanswered ones become documented assumptions.

## Questions

| # | Question | Working default | Config / code affected | Status |
|---|---|---|---|---|
| Q1 | Sample deal email; does it contain the customer contact email and the AE phone number? | Contact email in body; AE phone from `config/ae_directory.yaml` (else from the AE's reply) | `INTAKE_REQUIRED_FIELDS`, `config/ae_directory.yaml`, `agents/prompts/intake.md` | Pending; default **built** (P6) |
| Q2 | Dedicated CSM selection (Enterprise); pooled CSM as shared user (Growth); PM/Owner mapping | Round-robin pool; Growth pod user; CSM = Project Manager, CS Manager (Priya) = Project Owner | `config/templates.yaml`, `services/routing.py` | Pending |
| Q3 | Same ~15 tasks with different durations, or different task lists? | Same 15 tasks, different durations | Rocketlane templates | Pending |
| Q4 | Also confirm the customer name on the call? Retry limit and escalation target? | Confirm the customer name; 3 attempts; escalate to the CS Manager | Vapi prompt, `CALL_RETRY_DELAYS`, `ESCALATION_*` | Pending |
| Q5 | Also flag *blocked* tasks, or only the *overdue* rule? | Overdue only, as specified | Rocketlane automation | Pending |
| Q6 | Data-migration verification gate (customer sign-off before the phase closes)? | Include it | Rocketlane template (approval task) | Pending |
| — | Rocketlane access | Workspace provided: `karthicksriram.rocketlane.com` (Google login). API key goes in `.env` | `integrations/rocketlane.py` | Resolved |

## Assumptions (decided without asking)

1. Rocketlane replaces Asana. Existing Asana projects are not migrated.
2. The system handles new deals only. Backfilling existing customers is a production follow-up.
3. Tier comes from the AE call, per spec. Salesforce is not read.
4. Only two tiers exist. Anything else is treated as ambiguous.
5. Kickoff-call scheduling and the handoff summary are out of scope. The handoff summary is a stretch goal.
6. Slack Connect invites to the customer are mocked (paid feature). The internal channel, topic and message are real.
7. Duplicate onboarding project → hold and escalate. Never create a second one.
8. Rocketlane API down → bounded retries, then escalate. Never assume success.
9. Calls only within AE business hours (configurable window, Asia/Kolkata default).
10. Volume is low (~200 customers total), so a single process with SQLite and polling is appropriate.

## Root cause notes for the Part 1 doc

- **Template mix-ups:** the tier isn't in the deal email and the template is picked by hand. Fix: confirm the tier on the call with a read-back, verify it against the transcript, and pick the template in code from a fixed mapping. Tests guarantee Enterprise → 30-day/dedicated and Growth → 14-day/pooled.
- **Unverified data migration:** "done" is one click with no customer confirmation. Fix (pending Q6): a customer-approval task gates the phase.
- **Escalations falling through:** escalation depends on memory. Fix: a built-in Rocketlane automation (overdue 1d → PM, 4d → Owner).
