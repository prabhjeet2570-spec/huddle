# Validation record — 2026-10-04

- Python 3.12.13, macOS arm64, local PostgreSQL 16 container.
- 50 tests passed with `TEST_DATABASE_URL` set to the isolated `huddle_test` database.
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
the scripted checks. The [raw responses and checks](../artifacts/assistant-evaluation.json)
include exact requests, missing times/attendees, follow-up details, proposal edits,
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
screenshots. The assistant screenshot shows an actual live-model proposal.

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

The migrated implementation passed **56 automated tests**, including PostgreSQL
checkpoint pause/resume, six concurrent approvals producing one booking/outbox,
conflict alternatives with fresh approval, owner isolation, expiry, dismissal,
and reset. Existing booking and calendar-worker tests also passed.

The 13 live OpenRouter scenarios were rerun against LangGraph; all scripted checks
passed. `artifacts/assistant-evaluation.json` now records that run. These checks
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
