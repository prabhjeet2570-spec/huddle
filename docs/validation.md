# Validation record — October 4–5, 2026

The latest verification is the numbered-room section below: 68 automated tests,
16 live-model scenarios, four crash/recovery trials, and the populated profile
checks. Earlier sections are a chronological record; their room names and test
counts describe those earlier versions. `assistant-evaluation.json` is overwritten
by each run and currently contains the latest 16-scenario run, not the older runs.


- Python 3.12.13, macOS arm64, local PostgreSQL 16 container.
- Before the LangGraph migration, 50 tests passed with `TEST_DATABASE_URL` set to the isolated `huddle_test` database.
- Ruff lint/format checks and JavaScript syntax validation passed.
- Container image built from the lockfile.
- Full Compose stack started on alternate local ports; session creation, allocation,
  database readiness, UI delivery, and worker-to-calendar synchronization passed.
- Browser walkthrough: room discovery, booking, edit, activity history, assistant
  proposal and approval, and reliability metrics. Responsive view checked at 390px.
- [Raw recovery experiment](../artifacts/recovery-results.json) records the source
  commit and actual timing/output. It covers 24-way contention, 12 repeated requests,
  one lost-response trial, and three process-exit trials.

## Live model check

Provider: OpenRouter. Model: `openai/gpt-4o-mini`. Synthetic meeting data only.
Initial multi-turn messages produced a redundant question and a UTC/local wording
mistake. The system prompt was revised to reuse supplied details, immediately
prepare an approval proposal when complete, and use explicit local display times.
A fresh conversation then produced Cedar, four attendees, 10–11 AM local time,
with a correct proposal card. Clicking approval created the booking.

A subsequent live evaluation ran 12 scenarios, then repeated them with an
occupied-room scenario added. All 13 scenarios in the recorded second run passed
the scripted checks. The latest [raw responses and checks](../artifacts/assistant-evaluation.json)
cover these cases plus the expanded catalog, and include exact requests, missing times/attendees, follow-up details, proposal edits,
capacity overflow, unknown rooms, past dates, chat approval bypass attempts,
listing, cancellation requests, local-time conversion, and an occupied room.
Every case checks that chat created zero bookings; proposal cases check selected
expected fields. The occupied-room fixture is created separately through the API.
Manual response review found no false confirmation in this run.

Reproduce with the server's OpenRouter key in `.env`:

```sh
TEST_DATABASE_URL=postgresql://huddle:huddle@localhost:5438/huddle_test \
  uv run python scripts/evaluate_assistant.py
```

This is opt-in and spends real model tokens. It creates synthetic workspaces and
an occupied-room fixture in `huddle_test`, without truncating existing records.
Do not run concurrently with integration tests, which clear that database.
It overwrites the evaluation artifact. Dates advance with the run date, and local
UTC offsets are computed with the named timezone.

These are small, curated English scenarios for one model, not a representative
completion benchmark. Checks do not grade every word, field, or tool invocation.
DST ambiguity, multilingual requests, extensive prompt injection, provider outages,
and a statistically meaningful repeated-run evaluation remain uncovered here.
Unit/integration assistant tests use controlled model responses and are separate evidence.

## Screenshot provenance

The screenshots in `screenshots/` were captured from the running local application.
Room illustrations are locally authored SVG assets. Meetings are synthetic data
entered during the walkthrough. No dashboard values were hardcoded or edited into
screenshots. The current chat-proposal screenshot shows a live-model proposal; the assistant
and chat-confirmed screenshots show the server-generated confirmation after its
approval. Scripted conflict screenshots are separate evidence.

## History and repeatability

The development milestones were later split into smaller, dependency-ordered
commits. The raw artifact retains the original recorded source hash
`65ddb7fec18e55a223b89db4f4836ea6f481506a`; rewritten commit `1d71608` has an
identical Git tree. This was verified directly rather than changing historical
measurements. The first ten implementation milestones became twenty focused
commits; no dates or test results were backdated.

A separate Linux/arm64 container reproduction also passed the four fault trials.
The script accepts `HUDDLE_SOURCE_COMMIT` (or CI's `GITHUB_SHA`) for source archives
without a Git executable; otherwise it records `git rev-parse HEAD`.

The first hosted CI attempt failed in the recovery step after its unit/integration
suite passed. An unchanged-tree rerun at `b174a3c` passed the full workflow,
including the recovery script and image build. The initial failure's specific
cause was not established; it is not counted as a successful recovery trial.
Subsequent script diagnostics expose exceptions directly in CI annotations.

## LangGraph migration validation

The migrated implementation passed **57 automated tests**, including PostgreSQL
checkpoint pause/resume, six concurrent approvals producing one booking/outbox,
conflict alternatives with fresh approval, no-availability exhaustion, owner isolation, expiry, dismissal,
and reset. Existing booking and calendar-worker tests also passed.

The 13 live OpenRouter scenarios were rerun against LangGraph; all scripted checks
passed at that stage. The evaluation artifact has since been replaced by the
latest numbered-room run. These checks
retain the limitations described above; guided demos are not live-model evidence.

`uv run python scripts/workflow_demo.py` (with `TEST_DATABASE_URL` set) prepares a
pending approval in one Python process, exits it, and resumes in a fresh process.
The recorded [restart evidence](../artifacts/workflow-restart.json) shows a restored
approval pause, one reservation, and the same result on repeated approval. No model
calls are made. The script leaves synthetic records and does not truncate data.

Browser verification exercised a competing reservation, selected Maple as an
alternative, restarted the actual API process, refreshed the same browser session,
and approved the restored proposal successfully. The workflow panel showed
`reserved`. This verifies a committed pause across restart, not recovery of an
in-flight model call. Screenshots labeled LangGraph show the updated scripted demo.

The updated assistant UI was also checked at 390px with no horizontal overflow.
The updated Docker image built successfully with the existing pinned uv 0.9.9
builder and the frozen lockfile. The new CI restart step is configured; hosted CI
has not been run for these local commits.

## Functional and UI review

A browser check exposed a live-model contract failure: GPT-4o-mini supplied the
display name `Birch` instead of ID `birch`, repeated the invalid call, and exhausted
the budget. The tool schema now enumerates valid IDs, the prompt gives exact IDs,
and the proposal handler canonicalizes case/whitespace only for known catalog IDs.
Unknown rooms remain invalid. The repeated live request produced Birch, five
attendees, October 11, 2–3 PM local time, and approval created the reservation.
The suite now passes **58 tests**, including the capitalization regression.

The broader live set then exposed an occupied-room proposal: the booking constraint
would reject approval, but the proposal misleadingly offered an already busy room.
The handler now checks availability before saving a proposal. Returning a tool
error to the model still caused repeated calls in the first rerun (12/13 passed),
so an occupied-room response now ends deterministically with available alternatives
and no pending proposal. The final live rerun passed all **13 scenarios**; the raw
results replaced `artifacts/assistant-evaluation.json` at that stage; its current
contents are the later 16-scenario run. This does not hold a room;
approval still checks for later conflicts.
The occupied-room regression brings the automated suite to **59 passing tests**.

Browser checks covered search, direct create, reschedule, cancellation/history,
live model proposal and approval, competing-room alternatives, dismissal, and
New chat. New chat now clears request status as well as chat/proposal; a fresh
session refreshes the active page. Booking lists refresh calendar status periodically.
Changing a URL fragment now switches the visible page as well.
The running API returned 200 for health, readiness, rooms, and assistant status.
All four real HTTP/crash recovery trials passed again; raw results are saved in
`artifacts/ui-recovery-results.json` without replacing the original recovery record.
The screenshot set was refreshed to show the revised UI.

## Numbered rooms, sample profiles and in-page chat

The expanded catalog and UI passed **68 automated tests**, including SQL enforcement
for a 30-person room, SQL/application catalog consistency, default-disabled sample
profiles, and an idempotent persisted confirmation after assistant approval.

All **16 live OpenRouter scenarios** passed. The prompts now use numbered rooms;
additional cases exercise Room 105 (two people), Room 306 (16 people), and Room 404
(30 people). The over-capacity scenario requests 31 rather than 20 people because
20 is valid in the expanded catalog. Raw responses are in
`artifacts/assistant-evaluation.json`; curated checks are not general LLM accuracy.

[Profile scenario evidence](../artifacts/profile-scenarios.json) records 48 future
bookings across Blake, Morgan, Jake and Ashley, 40 capacity/time search combinations,
cross-owner read denial, cross-owner overlap rejection, stale edits and capacity
rejection. Each profile exercised six real HTTP 503s, retry exhaustion preserving
the room, and manual retry succeeding. Four historical fixtures were added explicitly
in SQL for the Past view; their IDs are in `artifacts/history-fixtures.json`.
Backoff clocks were accelerated; delayed work was held for 30 minutes. These are
controlled demonstrations, not latency benchmarks or evidence of real users.

The [expanded-catalog recovery rerun](../artifacts/expanded-catalog-recovery.json)
passed response-loss and all three worker-process death trials again, with one
remote calendar event per booking. The historical recovery artifact was preserved.

Browser checks covered four-profile switching, floor filtering, upcoming/past/
cancelled tabs, a live 16-person Room 306 proposal and approval, the persisted
assistant confirmation naming that room, and a scripted competing reservation.
Selecting Room 102 required a new approval and produced its own confirmation.
Desktop checks used 1280px; mobile directory and chat used 390px with body width
390px and drawer width 374px. Screenshots show those states in the README gallery.
The primary UI no longer shows the Workspace breadcrumb, simulator paragraph,
calendar explainer or completed-only latency claim. The provider/simulator boundary
remains documented in the README.

The recovery rerun records the checked-out baseline hash `e5ef98e`; worker/fault
changes were still uncommitted during execution and were subsequently committed
in `32ce785`. The recorded hash alone does not identify the complete working tree
used for that trial. The source field was preserved rather than relabeled.
