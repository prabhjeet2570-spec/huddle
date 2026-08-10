# Huddle

**A little room for good work.** Meeting-room booking with explicit AI approval,
PostgreSQL-enforced allocation, and calendar synchronization that recovers after
process and network failures.

![Huddle room discovery](docs/screenshots/rooms.jpg)

## What you can do

- Find rooms by time and capacity; inspect occupied and free time windows.
- Book, reschedule, or cancel. Conflicting edits leave the original reservation intact.
- Ask the OpenRouter-powered assistant to find a room and prepare a proposal.
  Only the **Approve & book** button executes that exact, expiring proposal.
- Inspect the booking's history and live synchronization state. Calendar failures
  leave the room reserved, retry automatically, and become visible review tasks
  when the retry budget is exhausted.

The UI, API, database, worker, and HTTP calendar simulator are runnable locally.
This is a portfolio/demo application, not a deployed production booking service.

## Run it

Requires Docker with Compose. Copy the example only if you do not already have an `.env`:

```bash
cp .env.example .env
docker compose up --build -d
```

Open **http://127.0.0.1:8010**. Enter a demo display name. API documentation is at
`/docs`; `/health` reports process liveness and `/ready` checks database/schema readiness.
If your installation provides the standalone command, use `docker-compose` instead.
The database and API bind to localhost. Stop with `docker compose down`; volumes
retain bookings and calendar state.

Only the assistant needs a model key. Set these in the server's gitignored `.env`:

```dotenv
OPENROUTER_API_KEY=your-key
OPENROUTER_MODEL=openai/gpt-4o-mini
```

Then recreate the API service: `docker compose up -d --force-recreate api`.
The key stays server-side. The model receives chat text and relevant tool results.
Room discovery and booking work without a key. A different model must support
OpenRouter tool calling.

### Native development

Requires Python 3.12+, [uv](https://docs.astral.sh/uv/), and PostgreSQL 16:

```bash
uv sync --frozen
docker compose up -d db
uv run python scripts/dev.py
```

The runner applies migrations and starts the API on 8010, the calendar simulator
on 8001, and the worker. Ctrl-C stops those processes. Restart after changing `.env`.
Run either the native application services or the complete Compose stack, not both
on the same ports.

## A short walkthrough

1. Choose tomorrow, a time window, and the number of people in **Find a room**.
2. Book a room and open **My bookings → Activity**. The reservation is immediate;
   calendar synchronization completes independently.
3. Reschedule it or cancel it. The previous slot is released transactionally.
4. Ask the assistant: “Prepare a proposal for Cedar tomorrow 10–11am, four people,
   titled Weekly planning.” Review the exact local time and approve.
5. Open **Reliability** to inspect the actual workflow records, retry counts,
   worker heartbeat, and synchronization latency.

![Assistant with a live-model proposal awaiting approval](docs/screenshots/assistant.jpg)

## Engineering decisions

**PostgreSQL owns allocation.** An exclusion constraint rejects overlapping
`[start, end)` ranges for the same room while allowing adjacent meetings.
Validation exists in the API for helpful errors, but independent writers still
face the database constraint. Optimistic versions reject stale edits. Creation
accepts an `Idempotency-Key`; reusing it with a different payload returns 409.

**Approval is a record, not a phrase matcher.** The assistant has read tools and
one proposal tool, with no direct booking-write tool. A proposal stores exact
arguments, owner, and a five-minute expiry. A new chat instruction or reset
supersedes pending proposals. Approval locks the proposal and owner session,
creates the reservation, and records the result in one transaction. Repeated
approval returns the same result.

**The calendar is eventually consistent.** A database trigger adds an outbox row
in the allocation transaction. A separate worker claims jobs with
`FOR UPDATE SKIP LOCKED` and a 30-second lease. A lost response or worker crash
leads to reconciliation against the same event ID. Versioned writes prevent an
old request from overwriting a newer calendar state. Six unsuccessful attempts
produce a review task; an owner can explicitly retry it.

**One deployable application, separate worker.** FastAPI serves a responsive
HTML/CSS/JavaScript UI. There is no frontend build service or agent framework;
the tool loop is small enough to inspect. PostgreSQL stores reservations,
approvals, histories, usage records, and pending work. The independent HTTP
simulator stores calendar events in SQLite.

More detail: [architecture and boundaries](docs/architecture.md).

## Measured evidence

The committed [raw run](artifacts/recovery-results.json) records the source commit,
Python version, machine architecture, every request latency, trial outcomes,
and unresolved work. It uses a dedicated PostgreSQL test database and a separate
HTTP calendar process. It makes no model calls.

| Workload | Observed outcome |
| --- | --- |
| 24 simultaneous requests for one slot | 1 reservation, 23 conflicts, 0 unexpected errors |
| 12 submissions using the same idempotency key | 1 logical reservation |
| Remote success followed by response timeout | Reconciled in 7.151s; 1 calendar event |
| Worker dies after claiming, before remote write | Recovered in 30.571s; 1 calendar event |
| Worker dies after remote success, before acknowledgment | Recovered in 30.581s; 1 calendar event |
| Worker dies after durable acknowledgment | Already complete; verified in 0.467s |
| Final allocation/work checks | 0 overlapping active pairs; 0 unresolved jobs |

These are **four individual fault trials**, not a reliability percentage or a
production benchmark. Times include the configured lease/retry delays and vary
by run. There is no claim of exhaustive fault coverage or a measured improvement
over a prior baseline.

The live OpenRouter walkthrough reached a correct proposal and an approved booking.
A subsequent [13-scenario live evaluation](artifacts/assistant-evaluation.json) passed
its scripted checks for interpretation, clarification, proposal edits, occupied rooms,
and approval boundaries. See [scope and reproduction](docs/validation.md#live-model-check).
This small curated set is not a general task-completion benchmark; review every proposal.

## Verify and reproduce

Unit tests need no database. Integration tests run only when `TEST_DATABASE_URL`
is set; skipped integration tests are not a full validation run.

```bash
uv run pytest -q
uv run ruff check app tests scripts
uv run ruff format --check app tests scripts
```

Create a **dedicated disposable** database ending in `/huddle_test`. Both the
integration fixtures and recovery experiment clear that database's application
records. They refuse a database URL with a different ending.

```bash
docker compose exec db createdb -U huddle huddle_test
export TEST_DATABASE_URL=postgresql://huddle:huddle@localhost:5438/huddle_test
uv run pytest -q
uv run python scripts/recovery_demo.py
```

The validated suite has **50 passing cases**, including actual PostgreSQL
contention, direct constraint enforcement, owner isolation, exact-action approval,
lease recovery, and retry exhaustion. The recovery script deliberately exits
worker subprocesses, waits for real lease expiry, and overwrites the raw results
artifact with the new run. Expect roughly one to two minutes.

GitHub Actions runs lint, formatting, the full database-backed suite, recovery
experiment, JavaScript syntax validation, and a container build. Recovery output
is uploaded as a CI artifact.

![Workflow history and synchronization metrics](docs/screenshots/reliability.jpg)

## Boundaries and next steps

- **Demo identity:** display names are not verified accounts. An opaque HttpOnly
  cookie scopes ownership to a seven-day browser session. Losing/signing out of
  that session loses access to its bookings. Real account recovery, SSO,
  organization membership, and administrative controls are not implemented.
- **Calendar simulator:** this verifies a declared versioned HTTP protocol, not
  Google/Microsoft calendar behavior. A real provider needs its own idempotency,
  reconciliation, authentication, and contract tests.
- **No temporary holds:** availability is advisory until the transaction commits.
  Another user may take the slot while a proposal is awaiting approval.
- **Scoped AI:** the assistant searches, lists, and proposes new bookings.
  Editing and cancellation use the UI. Each turn has at most three model calls,
  four tool calls per model response, and 700 output tokens per call; there is a
  30-turn hourly workspace limit. This is not a public-service abuse defense.
- **Limited evaluation:** four crash/timeout trials and 13 curated live-model
  scenarios do not establish production reliability or general language accuracy.
- **Demo catalog:** three static rooms with illustrative artwork. No real venue,
  organization, user adoption, delivered email, or uptime is claimed.
- **Deployment:** HTTPS, verified authentication, backup/restore procedures,
  global rate limits, retention policies, and real provider integration are
  required before public production use.

Room artwork is local SVG. UI screenshots show the actual running application,
including synthetic meetings created during browser validation.
