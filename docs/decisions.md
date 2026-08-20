# Decision record

Each entry states the options that were actually on the table, what was
chosen, and the cost that came with it. This exists so the reasoning is
recoverable later, including the parts that were not free.

---

## D1 — Exclusion constraint over per-slot unique rows

**Context.** Two agents must never double-book a room.

**Options.**

1. Explode each booking into 30-minute rows in a `booking_slots` table with
   `UNIQUE (room_id, slot_start)`, giving each row a `hold_id` and an expiry.
2. `SELECT ... FOR UPDATE` on the room row, then check and insert.
3. One `reservations` table with a `tstzrange` period and
   `EXCLUDE USING gist (room_id WITH =, period WITH &&)`.

**Chosen: 3.**

Option 1 works for plain bookings and shares the right instinct — let the
database arbitrate. Holds break it: expiry becomes a multi-row operation, and a
partially swept booking is an extra state to reason about. The exclusion
constraint expresses the actual rule ("no two live reservations for one room
may overlap") in a single declaration, and the partial `WHERE state IN
('held','confirmed')` lets cancelled and expired rows stay as an audit trail
without blocking anyone.

Row locking was rejected because it reintroduces exactly what we are trying to
remove: a read, a decision in Python, and a write, with correctness resting on
every future caller remembering to take the lock. The constraint cannot be
forgotten.

**Cost.** PostgreSQL only, and it needs the `btree_gist` extension because the
constraint mixes `=` on a scalar with `&&` on a range. SQLite has no
equivalent, so the test suite requires a real database. That is a real
inconvenience, accepted because testing this property against a different
engine would be testing something else.

---

## D2 — Holds as a state, not a separate table

**Context.** A hold and a booking both occupy a room.

**Options.** A `holds` table alongside `bookings`, or one table with a state
column.

**Chosen:** one `reservations` table; confirming is `held -> confirmed`.

This is what makes "the slot was taken between hold and confirm" structurally
impossible rather than merely unlikely. The row that blocks the room during
the hold is the same row that becomes the booking. There is no window in which
the room is unclaimed, so there is no race to lose.

**Cost.** One table carries two concepts, and every query must filter on
state. Worth it: the alternative needs a cross-table constraint that
PostgreSQL cannot express.

---

## D3 — Hold TTL of 120 seconds

**Options.** 30s, 120s, 600s.

**Chosen: 120 seconds** (`HUDDLE_HOLD_TTL_SECONDS`).

A hold blocks a real room for everyone, so it should be as short as the
conversation allows. The hold is placed once the user has named a room and
time, and released or confirmed on their next message. Two minutes covers a
person reading a confirmation and typing "yes"; 30 seconds loses a user who
gets distracted mid-sentence, and 10 minutes lets one indecisive person
sterilise a room through a whole standup.

**Cost.** A slow user loses their hold and may find the room gone. The failure
is visible and recoverable — they are told the hold lapsed and can place
another — which is better than the alternative failure, where a room silently
sits unusable.

---

## D4 — Which actions require confirmation

**Options.** Confirm every write; confirm nothing and rely on the prompt;
classify by blast radius.

**Chosen:** classify by blast radius (`ActionRisk`).

- `READ` — free. Nothing changes.
- `HOLD` — free. Reversible by construction: it releases itself.
- `HIGH` — `confirm_booking` and `cancel_booking`. Parked until the user
  agrees.

The distinction that carries the weight is that a hold is *self-reversing*.
That is what lets the agent act promptly — it can claim the room while the
user is still reading the summary — without ever taking an action a human has
to undo by hand.

Confirming everything would make the agent useless: it could not check
availability without permission. Confirming nothing leaves irreversible writes
to the model's judgement, which is precisely what the prompt cannot guarantee.

**Cost.** Every booking takes two user messages. That is the point.

---

## D5 — Deterministic confirmation matching, not an LLM classifier

**Context.** Something has to decide whether "sure, go for it" is a yes.

**Options.** A second LLM call; a deterministic phrase matcher.

**Chosen:** a deterministic matcher that fails closed
(`app/agent/confirmation.py`).

The confirmation gate exists *because* the model's judgement is not trusted for
irreversible actions. Routing the decision back through a model would defeat
it, and would be promptable: "the user said yes" is exactly the kind of thing
an injected instruction can assert. The matcher is auditable, instant, free,
and cannot be argued with.

It fails closed in two specific ways: an unrecognised phrase is ambiguous, and
a reply longer than six words is ambiguous even if it starts with "yes" —
because "yes but move it to room B" is a new instruction, not a confirmation.

**Cost.** Real coverage loss. An unusual phrasing gets asked again, which is
mildly annoying. For an action nobody can undo, annoying beats wrong.

---

## D6 — Retry ceiling of 3 attempts, 10s timeout, applied to model calls too

**Options.** No retries; 3 attempts; 5+ attempts with a circuit breaker.

**Chosen:** 3 attempts, 10-second per-attempt timeout, exponential backoff
from 200ms with ±10% jitter, capped at 5 seconds.

Retries are only worth it if the failure is transient, and a dependency that
fails three times in a row with backoff is usually down rather than busy. Past
that, retrying trades a fast, honest escalation for a slow one — while
consuming the conversation's budget and making the user wait.

Classification matters more than the ceiling: `HuddleError.retryable` is
checked before any retry, so a validation error or a missing room fails on the
first attempt. Retrying a terminal failure is pure waste, and it delays the
message the user actually needs.

Jitter exists so that N agents failing together do not all return at the same
instant and fail together again.

**The model is a dependency too.** The LLM call runs under the same policy. An
unprotected model call is the easiest reliability hole to leave open, because
it only shows up when the provider has a bad day: the SDK exception propagates
out of the graph and the user gets an HTTP 500 with a stack trace instead of a
sentence. Wrapping it means a 429 is retried, a 401 is not, and either way the
turn ends with an escalation the user can read.

**Provider errors are classified by HTTP status, not exception type.** Status
is a stable contract; SDK class hierarchies are not, and some SDKs subclass
`ConnectionError` for errors that are anything but transient. Classifying on
type would make a library upgrade able to silently change retry behaviour.

**Cost.** No circuit breaker, so a hard-down dependency is retried afresh by
every conversation. Acceptable at this scale; the alerts make it visible.

---

## D7 — Hold-cycling thresholds

**Context.** Expiring holds create an exploit: never confirm, and re-hold the
instant the last one lapses. Each request is individually legitimate.

**Chosen.** Over a 30-minute rolling window, with at least 4 holds:

| Signal | Threshold | Catches |
|---|---|---|
| Confirm ratio | ≤ 0.34 | The patient squatter |
| Hold frequency | ≥ 10 per window | The fast cycler whose ratio has not dropped yet |

Rate limit on breach: 15 minutes.

**Why these numbers.** The minimum of 4 holds is the important one — without
it, a user who places two holds and abandons one scores 0.5 and gets flagged
for ordinary indecision. The 0.34 ratio means "confirmed fewer than one in
three", which is hard to reach by accident and hard to avoid while squatting.
The two signals are independent because a cycler ramping up has a good ratio
right until they do not.

**Measured** against 14 labelled sessions in
`tests/services/test_hold_cycling.py`: **precision 1.00, recall 1.00**. The
labelled set deliberately includes unflattering negatives — users who abandon
a third of their holds — because those are what a naive threshold flags.

**Asymmetry, on purpose.** A false positive rate-limits a legitimate user; a
false negative means a squatter gets caught one window later. Precision is
therefore asserted at 1.0 and recall is allowed to lag.

**Cost.** A genuinely indecisive heavy user can be limited. The limit is a
15-minute brake, not a ban, and the evidence is stored with the decision.

---

## D8 — Rate limits do not lift when the window empties

**Context.** If an active limit were re-evaluated from the rolling window, a
cycler could stop for one window, watch their ratio reset, and resume.

**Chosen:** an active rate limit short-circuits evaluation entirely. Recovery
needs *both* the cooldown to elapse and the offending holds to age out.

**Cost.** Slightly harsher than it first appears, which is why the behaviour
is asserted explicitly in a test rather than left as an emergent property.

---

## D9 — Server-side conversation state, no LangGraph checkpointer

**Context.** Something has to remember what was said. The cheapest option is to
have `/chat` accept the full transcript from the client on every turn.

**Options.** Client-supplied history; `langgraph-checkpoint-postgres`; own
tables.

**Chosen:** own `conversations` / `conversation_messages` tables.

Client-supplied history has two defects that rule it out. Tool calls and
results get dropped, so on turn 3 the model cannot see that it booked a room on
turn 1. And the client is trusted to report what the assistant said, which is
not a trust boundary worth having when the assistant's previous statements
influence irreversible actions.

The LangGraph checkpointer would have fixed persistence, but budgets,
escalation status, pending confirmations and outcome tagging all need to be
queryable alongside the transcript, and the metrics read from those tables. An
opaque checkpoint blob would have meant maintaining a second store anyway.

**Cost.** More code than importing a checkpointer, and no free time-travel
debugging.

---

## D10 — Metrics computed from durable tables

**Options.** In-process counters or a Prometheus registry; compute from rows.

**Chosen:** compute from `turns`, `tool_invocations`, `sagas`, `reservations`.

A metric you can recompute is one you can audit and one that survives a
restart. Every number in `/metrics` and `scripts/report_metrics.py` traces
back to rows, and each carries its own definition string — reliability metrics
are too easy to report flatteringly by accident.

**Cost.** Reads hit the database, so this is not a scrape-every-second
endpoint. Fine: these are engineering metrics, not a dashboard feed.

---

## D11 — Deterministic failure injection, keyed by call site

**Options.** Random chaos injection; mock patching per test; a keyed injector.

**Chosen:** a keyed injector armed on exact call counts
(`app/reliability/failure_injection.py`).

Chaos you cannot reproduce is not a test. `injected(Scenario.CALENDAR_FAILURE,
"calendar.create", on_calls=(1,))` fails precisely the first attempt, every
run, which is what lets CI assert "retryable failures do not abort a booking"
rather than hoping.

It is inert unless `HUDDLE_FAILURE_INJECTION_ENABLED` is set, so it cannot arm
itself in production. That is asserted by a test.

**Cost.** Call sites carry an `injector.maybe_fail("name")` line — a small
amount of test scaffolding in production code, accepted because it is the only
way to inject at a real boundary rather than at a mock.

---

## D12 — Async throughout

**Options.** Keep the synchronous stack; go async.

**Chosen:** async, with `asyncpg`.

Timeouts (`asyncio.wait_for`), the concurrency test (50 genuinely simultaneous
attempts), the background hold sweeper, and clean OTel span nesting are all
markedly simpler async. Doing it in one pass early was cheaper than
retrofitting it around instrumentation later.

**Cost.** Alembic still needs a synchronous driver, so `psycopg` ships
alongside `asyncpg` and `Settings.sync_database_url` translates between them.

---

## D13 — Provider-agnostic model layer

**Chosen:** `HUDDLE_LLM_PROVIDER` selects `openai`, `anthropic`, or `fake`.

The provider is configuration. Everything downstream speaks the LangChain
message interface. `fake` matters most: `FakeChatModel` replays a scripted
sequence of tool calls, which is what makes the agent loop testable in CI with
no API key, no network and no bill — including malformed and looping calls a
real model produces only occasionally.

Provider SDK retries are disabled (`max_retries=0`) so that retry policy lives
in one place.

---

## D14 — Escalation on saga failure even when rollback is clean

**Context.** A booking rolled back cleanly. Is that an error?

**Chosen:** escalate. `SagaFailed` marks the conversation as needing human
review and writes an alert.

State being clean is not the same as the user being served. Somebody asked for
a room and did not get one for a reason they cannot act on, and a dependency
failed three times. If rollback *also* failed, the alert is `critical`, since
that is the one case where the database is genuinely inconsistent.

**Cost.** Escalation rate reflects dependency health, not just agent quality.
The metrics report both, so the two are separable.
