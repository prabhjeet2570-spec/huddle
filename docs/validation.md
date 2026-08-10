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

This was a functional smoke check, not a fixed evaluation set. Do not turn it into
a completion percentage or claim broad natural-language correctness. Unit/integration
assistant tests use controlled model responses and are separate evidence.

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
