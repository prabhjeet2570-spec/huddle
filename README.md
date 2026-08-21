# Huddle

An LLM agent that books meeting rooms over an HTTP API.

Booking a room is an irreversible write to a resource other people are
competing for, performed by a model that can hallucinate a room, loop without
converging, or decide on its own that the user said yes. Huddle is the set of
mechanisms that make that safe: a database constraint that makes double-booking
impossible, a saga that unwinds half-finished bookings, retry logic that knows
what is worth retrying, a confirmation gate the model cannot talk its way past,
and abuse detection for the exploit that expiring holds create.

**Stack** — Python 3.12 · FastAPI · LangGraph · PostgreSQL 16 · SQLAlchemy 2.0
(async) · Alembic · OpenTelemetry · Streamlit · Docker Compose · GitHub Actions
· 203 tests

---

## Contents

- [Quick start](#quick-start) · [Running without an API key](#running-without-an-api-key)
- [Measured results](#measured-results)
- [How it works](#how-it-works): [concurrency](#1-concurrency), [compensation](#2-compensation),
  [retries](#3-dependency-failures), [guardrails](#4-guardrails),
  [hold cycling](#5-hold-cycling), [observability](#6-observability),
  [failure injection](#7-failure-injection), [metrics](#8-metrics)
- [API reference](#api-reference) · [Configuration](#configuration) · [Project layout](#project-layout)
- [Testing and CI](#testing-and-ci) · [Limitations](#limitations)

---

## Quick start

```bash
cp .env.example .env          # works as-is
docker compose up -d          # Postgres + API + Streamlit UI

curl localhost:8000/health
open http://localhost:8501    # sign in: acme / alice / huddle-dev-password
```

Interactive API docs are at `http://localhost:8000/docs`.

Local development without Docker for the app:

```bash
docker compose up -d db
pip install -r requirements-dev.txt
alembic upgrade head
uvicorn app.main:app --reload

pytest                                    # 203 tests
python -m scripts.benchmark_concurrency   # the double-booking guarantee
python -m scripts.simulate --sessions 40 --keep && python -m scripts.report_metrics
```

### Running without an API key

Only `/chat` needs a model. Without a key it returns a clear message saying so,
and everything else runs:

| | Needs an API key |
|---|---|
| REST API — hold, confirm, cancel, availability, schedules | no |
| The concurrency guarantee and its benchmark | no |
| The 203-test suite, including failure injection | no |
| Metrics, simulation, traces | no |
| `/chat` conversational agent | **yes** |

The test suite drives the real graph, executor, guardrails and saga against a
scripted model (`HUDDLE_LLM_PROVIDER=fake`), so a key buys live language
understanding, not test coverage. To enable chat, set `HUDDLE_LLM_API_KEY` and
`HUDDLE_LLM_PROVIDER` (`openai` or `anthropic`).

---

## Measured results

Every number below is reproducible from the repository. None are estimates.

**Concurrency** — `python -m scripts.benchmark_concurrency --attempts 50`

```
concurrent attempts     50
succeeded                1
rejected by constraint  49
unexpected errors        0
rows occupying the room  1
wall clock             800 ms
RESULT: PASS - exactly one winner
```

**Hold-cycling detection** — 14 labelled sessions,
`pytest tests/services/test_hold_cycling.py -s`

```
TP=6  FP=0  TN=8  FN=0   precision=1.00  recall=1.00  f1=1.00
```

**Agent metrics** — 48 simulated conversations, `python -m scripts.report_metrics`

| Metric | Value | |
|---|---|---|
| task_completion_rate | 0.875 | 42/48 |
| escalation_rate | 0.125 | 6/48 |
| wrong_tool_call_rate | 0.153 | 19/124 |
| compensation_frequency | 0.240 | 6/25 |
| compensation_success_rate | **1.000** | 6/6 |
| turn_latency_p50 | ~30 ms | varies per run |
| turn_latency_p95 | ~660 ms | varies per run |
| tool_calls_per_confirmed_booking | 6.5 | 124/19 |

The simulation is seeded, so the ratios reproduce exactly; the latencies are
wall-clock and move between runs. The p95 sits above the p50 because turns that
run a full saga do roughly four times the work of a read.

The workload deliberately injects conflicts, malformed tool calls, hallucinated
tool names and calendar failures. That is why completion is 0.875 rather than
1.0 — a perfect score here would mean the workload exercises nothing.

---

## How it works

### 1. Concurrency

Double-booking is prevented by one declaration:

```sql
EXCLUDE USING gist (room_id WITH =, period WITH &&)
    WHERE (state IN ('held', 'confirmed'))
```

No code path reads availability and then writes. Every hold is an `INSERT`, and
PostgreSQL decides whether the row may exist. `RoomNotAvailable` is not an error
to be avoided — it is the mechanism.

Three properties follow from the constraint rather than from application code:

- `period` is a `tstzrange` with `'[)'` bounds, so a meeting ending at 11:00
  frees the room at 11:00 and adjacency is never mistaken for overlap.
- The partial `WHERE` lets cancelled and expired rows stay in the table as an
  audit trail without blocking anyone.
- The constraint is scoped by `room_id`, so two tenants can hold their own room
  "A" at the same instant.

**Holds are a state, not a separate table.** A hold is a reservation in state
`held` with a TTL; confirming it is `held → confirmed` on the same row. That is
what makes "the slot was taken between hold and confirm" structurally
impossible — the row that blocks the room during the hold is the row that
becomes the booking, so the room is never briefly unclaimed.

Holds expire, but correctness does not depend on the sweeper: `place_hold`
clears the room's lapsed holds inside the same transaction as its own insert.
The background sweeper only keeps availability queries honest.

See [D1, D2, D3](docs/decisions.md).

### 2. Compensation

Confirming a booking is four steps across three systems:

```
place_hold ──▶ confirm ──▶ notify ──▶ calendar sync
                  ▲           ▲            │ fails
                  └───────────┴────────────┘
                     compensate in reverse
```

Only the first two are ours. A failed calendar sync would otherwise leave a
confirmed room that no external system knows about; a failed notification leaves
attendees unaware of a meeting that now exists. Neither heals itself.

So it runs as a saga. A failure at step *k* undoes steps *k-1 … 1* in reverse
and returns the reservation to released. Compensations are idempotent — running
one twice reports "nothing to undo" and is logged as `NOOP` rather than failing
— and every forward step and compensation is written to `saga_events`.

A compensation that itself fails does not strand the rest of the unwind, and
raises a critical alert, since that is the one case where state really is
inconsistent.

`tests/services/test_compensation.py` injects a failure at each step and asserts
the room is released, no notification stands, and no calendar entry survives.

### 3. Dependency failures

```python
class HuddleError(Exception):
    retryable: bool = False     # the single source of truth
```

Every tool call and every model call runs under a 10-second deadline with up to
3 attempts and exponential backoff (200 ms, ×2, capped at 5 s, ±10% jitter).
Classification matters more than the ceiling: a timeout or a 5xx is retried,
while a validation error or a missing room fails on the first attempt. Retrying
a terminal failure burns the conversation's budget and delays the message the
user actually needs.

Provider SDK errors are classified by HTTP status, not exception type, so an SDK
upgrade that introduces a new exception class cannot silently become retryable:

| Status | Retried | |
|---|---|---|
| 429, 408, 5xx | yes | the provider is busy or broken |
| 401, 403 | no | the key will still be wrong on attempt three |
| 400, 404, 422 | no | the request is malformed, not unlucky |

When retries are exhausted the conversation is marked `escalated` and an alert
row is written to the review queue at `GET /alerts`. A provider outage returns a
sentence and HTTP 200, not a stack trace and a 500:

```json
{ "status": "escalated",
  "response": "I cannot reach the assistant service right now, so I have
               stopped rather than guess. Nothing was changed." }
```

### 4. Guardrails

**Blast radius.** Every tool declares an `ActionRisk`:

| Risk | Tools | Behaviour |
|---|---|---|
| `READ` | `list_rooms`, `list_available_rooms`, `get_room_schedule`, `list_my_bookings` | Runs freely |
| `HOLD` | `place_hold` | Runs freely — self-reversing |
| `HIGH` | `confirm_booking`, `cancel_booking` | Parked until the user confirms |

Holds are self-reversing, which is what lets the agent act promptly — claiming
the room while the user reads the summary — without ever taking an action a
human has to undo by hand.

A `HIGH` call is not executed. Its validated arguments are stored and replayed
verbatim once the user agrees, so what they consent to is exactly what runs.

**Confirmation is matched deterministically, not by an LLM.** The gate exists
because the model's judgement is not trusted here, so routing the decision back
through a model would defeat it. It fails closed:

```
"yes" / "go ahead" / "book it"                      → AFFIRM
"no" / "not yet" / "cancel that"                    → DECLINE
"yes but move it to room B and start an hour later" → AMBIGUOUS  ← asked again
```

**Budgets.** 25 tool calls and 60k tokens per conversation. A breach halts the
turn and escalates. The graph's `guard` node runs before the model, so a
conversation already over budget never reaches the LLM. This is also what bounds
a model that loops without converging. Rejected and malformed calls still
consume budget; otherwise a model emitting nothing but garbage loops for free.

**Validation.** Every tool argument is parsed by a Pydantic schema before the
tool body runs. Failures return the specific error so the model can correct
itself:

```
Status: error
Message: Invalid arguments for place_hold: starts_at: Value error, must land
on a 30-minute boundary with zero seconds. Fix them and call the tool again.
```

### 5. Hold cycling

Expiring holds fix one problem and create another.

**The exploit.** A hold blocks a room and releases itself, so a user who never
confirms can occupy a room indefinitely by placing a fresh hold the instant the
last one lapses. Every individual request is legitimate; only the pattern is
not, and nothing in a single request reveals it.

**The detection.** Two independent signals over a 30-minute rolling window on
the `hold_events` audit trail, requiring at least 4 holds before either can
fire:

| Signal | Threshold | Catches |
|---|---|---|
| Confirm ratio | ≤ 0.34 | The patient squatter |
| Hold frequency | ≥ 10 / window | The fast cycler whose ratio hasn't dropped yet |

Past either threshold, new holds are rate-limited for 15 minutes and the
detection is logged with its evidence: the counts, the ratio, the window bounds,
and which rule fired.

Two details took a second pass to get right:

- The minimum of 4 holds keeps ordinary indecision out. Without it, a user who
  places two holds and abandons one scores 0.5 and gets flagged.
- An active rate limit short-circuits re-evaluation. Otherwise a cycler could
  pause for one window, watch their ratio reset, and resume. Recovery requires
  both the cooldown to elapse and the offending holds to age out.

Measured against 14 labelled sessions including deliberately unflattering
negatives (users who abandon a third of their holds): **precision 1.00, recall
1.00**. The test asserts precision at 1.0 and allows recall to lag, because a
false positive rate-limits a real user while a false negative just means
catching the squatter one window later.

### 6. Observability

OpenTelemetry spans following the GenAI semantic conventions, exported over
OTLP/HTTP. The span tree makes the reasoning → tool → observation loop readable
without grepping logs:

```
huddle.turn                          gen_ai.conversation.id, huddle.conversation.outcome
├── chat gpt-4o-mini                 gen_ai.usage.input_tokens / output_tokens
├── execute_tool place_hold          gen_ai.tool.name, huddle.tool.risk, .attempts
├── chat gpt-4o-mini
└── execute_tool confirm_booking
    ├── saga confirm_booking.confirm_reservation
    ├── saga confirm_booking.notify_attendees
    └── saga confirm_booking.sync_calendar
```

Every trace carries a conversation id and, once terminal, an outcome:
`completed`, `escalated`, `failed` or `blocked`.

```bash
docker compose --profile observability up -d   # local OTLP collector on :4318
HUDDLE_OTEL_ENABLED=true
```

### 7. Failure injection

Failures that cannot be reproduced are not tests. The injector is keyed by call
site and fires on exact call counts:

```python
with injected(Scenario.CALENDAR_FAILURE, "calendar.create", on_calls=(1,)):
    ...  # fails precisely the first attempt, every run
```

Scenarios: tool timeout, tool 500, malformed response, slot taken between hold
and confirm, notification failure, calendar failure, compensation failure. Each
runs in CI and asserts the same invariant — no state corruption: the room
released, no notification standing, no orphaned calendar entry.

The injector is inert unless `HUDDLE_FAILURE_INJECTION_ENABLED` is set, so it
cannot arm itself in production. A test asserts that.

### 8. Metrics

Everything in `GET /metrics` and `scripts/report_metrics.py` is computed from
durable tables — `turns`, `tool_invocations`, `sagas`, `reservations` — never
from in-process counters. A metric that can be recomputed can be audited, and it
survives a restart. Each one carries its own definition string, because
reliability metrics are easy to report flatteringly by accident.

---

## API reference

All booking and chat routes require `Authorization: Bearer <token>` from
`POST /auth/login`. Every query is scoped to the caller's organization.

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/auth/login` | Exchange org, username and password for a JWT |
| `GET` | `/auth/me` | The current user and organization |
| `GET` | `/bookings/rooms` | Rooms in the organization |
| `GET` | `/bookings/available` | Rooms free for a given window |
| `GET` | `/bookings/me` | The caller's reservations |
| `POST` | `/bookings` | Place a hold — `409` if the slot is taken |
| `POST` | `/bookings/{reference}/confirm` | Run the confirmation saga |
| `DELETE` | `/bookings/{reference}` | Cancel a reservation |
| `POST` | `/chat` | One conversational turn (needs an API key) |
| `GET` | `/health` | Liveness and database check |
| `GET` | `/metrics` | Reliability metrics, computed from tables |
| `GET` | `/alerts` | Human-review queue: escalations and failed compensations |

`/chat` takes a conversation id, not a transcript. History lives server-side —
asking the client to replay it is the obvious shortcut and a bad one, because it
drops tool calls (on turn three the model can no longer see that it booked a
room on turn one) and it trusts the caller to report what the assistant said.

## Configuration

Every setting is read from the environment with the `HUDDLE_` prefix, parsed by
`app/config.py`. See [.env.example](.env.example) for the full list; the ones
worth knowing:

| Variable | Default | Meaning |
|---|---|---|
| `HUDDLE_DATABASE_URL` | `postgresql+asyncpg://huddle:huddle@localhost:5433/huddle` | PostgreSQL only |
| `HUDDLE_JWT_SECRET` | — | Signing key; change it outside development |
| `HUDDLE_LLM_PROVIDER` | `openai` | `openai`, `anthropic` or `fake` |
| `HUDDLE_LLM_API_KEY` | — | Required only for `/chat` |
| `HUDDLE_HOLD_TTL_SECONDS` | `120` | How long a hold blocks a room |
| `HUDDLE_TOOL_TIMEOUT_SECONDS` / `_MAX_ATTEMPTS` | `10` / `3` | Per-call deadline and retry ceiling |
| `HUDDLE_MAX_TOOL_CALLS_PER_CONVERSATION` | `25` | Budget that bounds a non-converging model |
| `HUDDLE_MAX_TOKENS_PER_CONVERSATION` | `60000` | Token budget for the same reason |
| `HUDDLE_ABUSE_*` | window `1800`s, min 4 holds, ratio `0.34` | Hold-cycling thresholds |
| `HUDDLE_OTEL_ENABLED` | `false` | OTLP export |
| `HUDDLE_FAILURE_INJECTION_ENABLED` | `false` | Tests only; never enable in production |

## Project layout

```
app/
├── api/              routes, auth, dependencies
├── agent/            graph · executor · tools · confirmation · runner
├── services/         booking · saga · abuse detection · sweeper · notifications
├── reliability/      retry · saga runner · budget · failure injection
├── domain/           pure logic: rules · time_range · schedule (no I/O)
├── infrastructure/   SQLAlchemy models and repositories
└── observability/    OpenTelemetry setup and span helpers

ui/                   Streamlit client
scripts/              benchmark_concurrency · simulate · report_metrics
migrations/           Alembic
tests/                domain · services · agent · concurrency · failure_injection
```

Requests flow inward: `api → agent → services → reliability → domain`, with
`infrastructure` reached only through repositories.

The agent graph is not the stock ReAct loop:

```
START ─▶ guard ─▶ agent ─▶ tools ─▶ agent ─▶ END
           │        └── no tool calls ──────▶ END
           └── over budget ─▶ END       tools ─▶ escalation_notice ─▶ END
```

`guard` can end a turn before the model is called. `tools` routes through the
executor rather than LangGraph's `ToolNode`, so validation, blast-radius gating,
retries and escalation all apply. State carries the budget and the outcome, so a
turn's disposition is computed rather than inferred afterwards.

Huddle is multi-tenant: organizations own rooms, users and reservations, the JWT
carries the organization, and every query is scoped by it.

---

## Testing and CI

```bash
pytest                              # 203 tests
pytest tests/concurrency -s         # the double-booking guarantee
pytest tests/failure_injection -s   # no state corruption under injected failure
pytest -m "not postgres"            # pure domain tests, no database
```

| Area | What it covers |
|---|---|
| `tests/concurrency/` | 50-way contention, adjacency, partial overlap, tenant isolation |
| `tests/services/test_compensation.py` | Failure at each step, reverse order, idempotency |
| `tests/services/test_hold_cycling.py` | Thresholds, cooldown, measured precision/recall |
| `tests/agent/test_guardrails.py` | Blast radius, validation, budgets, tenant isolation |
| `tests/agent/test_agent_loop.py` | Full loop with a scripted model, confirmation, non-convergence |
| `tests/failure_injection/` | Every scenario, asserting clean state |
| `tests/domain/` | Rules at their boundaries, no database |

The suite runs against real PostgreSQL, because the property under test is
enforced by a PostgreSQL exclusion constraint and has no SQLite equivalent.
Testing it against another engine would be testing something else.

[GitHub Actions](.github/workflows/ci.yml) runs `ruff check`, `ruff format
--check`, the test suite against a PostgreSQL 16 service container, and a Docker
build. The concurrency and failure-injection suites are separate jobs so a
regression in either safety property shows up in the job list instead of being
buried in a 200-test run. CI also re-runs the concurrency benchmark and
regenerates the metrics, so the numbers in this README cannot quietly go stale.

## Documentation

- [docs/decisions.md](docs/decisions.md) — every significant choice, the options
  weighed, and the cost accepted
- [docs/assumptions.md](docs/assumptions.md) — domain assumptions and their
  consequences
- [docs/business-rules.md](docs/business-rules.md) — the rule catalogue

## Limitations

- **No circuit breaker.** A hard-down dependency is retried afresh by every
  conversation. Alerts make it visible; nothing suppresses the traffic.
- **The confirmation matcher is a phrase list.** It fails closed, so the failure
  mode is asking again rather than acting wrongly, but unusual phrasings do get
  asked twice.
- **Notification and calendar sync are simulated**, writing to local tables.
  They are real enough to fail, retry and compensate against, but they are not
  integrations.
- **PostgreSQL only**, by design. The exclusion constraint has no portable
  equivalent.
- **The hold sweeper runs in-process.** Multiple replicas are safe
  (`FOR UPDATE SKIP LOCKED`), but there is no distributed scheduler.
- **Metrics query the database on request**, so `/metrics` is not a
  scrape-every-second endpoint.
