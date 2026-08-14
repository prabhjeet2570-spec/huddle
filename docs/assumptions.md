# Domain assumptions

Booking a room touches a lot of unstated policy: how long a meeting may run,
what "available" means, whether weekends count. Each assumption below is
recorded with its justification and, more usefully, what it forces the system
to do.

Implementation trade-offs live in [decisions.md](decisions.md); this file is
only about the domain.

## Room capacities

Default seed: A=4, B=6, C=8, D=12, E=20.

Uneven capacities are deliberate. A request for ten people leaves only two
candidate rooms, so availability search has to filter on capacity as well as
time — a more honest exercise than five identical rooms.

Capacities are per organization and live in the database, not in configuration.
A second tenant in the seed has different rooms under the same names, which is
what makes tenant isolation demonstrable rather than merely claimed.

## Business hours

08:00 to 20:00, Monday to Friday.

Some bound is necessary: without one, a day's schedule is 48 slots, half of
them overnight, which makes the conversational rendering useless.

## Timezone

A fixed UTC-3 offset rather than `America/Montevideo`. Uruguay does not observe
daylight saving, so a fixed offset is exact and avoids a tzdata dependency in
the container.

Everything is stored as PostgreSQL `timestamptz` and converted to office time
on the way out. This matters more than it looks: a database that silently drops
the offset on write and has it re-attached on read is correct only for as long
as both sides keep making the same assumption. Storing the offset removes that
coupling.

## Interval semantics

Half-open: `[start, end)`. A meeting ending at 11:30 leaves the room free at
11:30.

This is load-bearing. The `tstzrange` column uses `'[)'` bounds, so the
exclusion constraint and the domain's `TimeRange.overlaps` cannot disagree
about whether two meetings touch or overlap.

## Bookings must be in the future

A booking cannot start in the past, and cannot be cancelled at or after its
start time — once a meeting has begun, cancelling frees nothing useful.

Both rules need the current time, which the agent does not inherently know. It
is injected into the system prompt, and independently enforced by the service
against an injected clock.

## Working days

Monday to Friday, consistent with the 08:00–20:00 office hours.

## Booking horizon

90 days. Without a limit, availability searches accept unreasonable ranges, and
booking beyond a quarter ahead has little operational value.

## Minimum attendees

One. A booking for zero people is not a meeting.

A request for more people than the largest room holds is rejected with the
actual maximum rather than a generic refusal, so the user can act on it.

## What "available" means for a range

A room is available only if it is free for the *entire* requested range.

Someone asking what is free from 14:00 to 17:00 wants a three-hour meeting; a
room with scattered gaps inside that window is not useful, and listing it adds
noise. `get_room_schedule` answers the other question — the occupied and free
periods of one room on one day.

If nothing is free for the whole range the result is empty, and the error names
the conflict and the alternatives.

## Holds

A hold is a provisional reservation with a TTL. It exists because the gap
between "the user chose a room" and "the user confirmed" is exactly the window
in which someone else can take it. Holding closes that window.

Two consequences the domain has to live with:

- A hold blocks a real room for everyone else, so its TTL is deliberately
  short (120 seconds).
- Because holds expire, they can be cycled. That exploit and its detection are
  documented in [decisions.md](decisions.md) D7 and D8.

## Out of scope

Recurring bookings, participant invitations, and editing a booking in place.
Cancel and rebook instead.

Notification and calendar sync *are* implemented, but as local simulations.
They are real enough to fail, retry and compensate against — which is their
purpose here — but they are not integrations with real providers.
