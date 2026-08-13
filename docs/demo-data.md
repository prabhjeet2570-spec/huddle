# Populated local walkthrough

Set `HUDDLE_DEMO_PROFILES=true` in the gitignored `.env` and restart Huddle. The
**Viewing as** selector opens Blake, Morgan, Jake, or Ashley. Each owns a separate
sample session; room availability is shared. This convenience is explicitly local
and is disabled in `.env.example`. It is not verified identity or authentication.
Switching to a profile rotates that profile's cookie token, so a second browser
using the same profile needs to switch again. Normal display-name sessions retain
the existing ownership behavior.

The sample directory contains 24 rooms numbered by floor: 101–106, 201–206,
301–306, and 401–406. Capacities range from 2 to 30; floor is catalog metadata,
not inferred from capacity. Legacy internal IDs preserve existing booking records.
These are fictional spaces, not NYU room numbers.

`scripts/populate_demo.py` creates twelve future sample bookings per profile through
HTTP APIs, records edits and cancellations, and exercises actual worker requests
against the independent calendar service. It adds one explicitly historical fixture
per profile for the Past tab; historical fixtures are SQL inserts, not proof that
past-date user requests are accepted. The normal API still rejects those requests.
Idempotency keys prevent a same-day rerun from duplicating the original bookings.
Nothing is truncated. The script leaves demonstration records in the local database.

Run against native services while the normal calendar worker is stopped, so only
this runner claims the fault-trial jobs. For example, start the API and calendar
separately, enable sample profiles, and run:

```bash
uv run python scripts/populate_demo.py
```

Then start `uv run python -m app.worker` again. The normal `scripts/dev.py` runner
starts all services including the worker; stop it first if using the separate
process setup. Default ports are 8010 for API and 8001 for calendar.

The recorded [scenario results](../artifacts/profile-scenarios.json) contain:

- 48 future bookings, varying rooms, capacities, 30/60/90-minute windows and dates.
- Six consecutive injected HTTP 503s for each profile, preserving allocation.
- Owner-triggered retry completing successfully, followed by a second exhausted
  task left visible as **Update needs attention**.
- One queued task per profile deliberately postponed for 30 minutes. Its Pending
  state is temporary; normal worker execution eventually updates it.
- Cross-profile read denial, shared-room overlap rejection, over-capacity rejection,
  stale edit rejection, and 40 time/capacity search combinations.

Backoff clocks were accelerated in SQL for the controlled outage trials. The
outage is fault injection; these numbers are not a production latency benchmark.
The four [historical fixtures](../artifacts/history-fixtures.json) were added in a
separate run. The live browser also added a 16-person Room 306 reservation through
OpenRouter, explicit approval, and the real booking transaction.

Calendar latency remains available from `/metrics` for engineering investigation.
It is absent from the product UI: completed-only p95 does not describe exhausted
updates or establish reliability. Provider limitations and fault results belong
in the README and validation record.
