# Business rules

Enforced by `app/domain/rules.py` and `app/services/booking_service.py`, and
covered at their boundaries by `tests/domain/test_rules.py`.

The system prompt describes these rules so the conversation goes smoothly. It
does not enforce them. Every rule below is checked server-side on arguments
that arrived from a language model, and the two most important guarantees —
no overlap, and no unauthorised irreversible action — are enforced below the
service layer entirely.

## Catalogue

| | Rule | Boundary cases tested |
|---|---|---|
| R1 | Bookings must not start in the past | Exactly now is accepted; one slot earlier is rejected |
| R2 | Monday to Friday only | Friday accepted; Saturday and Sunday rejected |
| R3 | Within business hours | 08:00 start and 20:00 end accepted; crossing midnight rejected |
| R4 | Aligned to the 30-minute grid, positive duration | 10:00–10:30 accepted; 10:15 rejected; zero-length rejected |
| R5 | At most 3 hours | Exactly 3h accepted; 3h30 rejected |
| R6 | At least one attendee | 1 accepted; 0 rejected |
| R7 | Room capacity covers attendees | Equal to capacity accepted; the error states the actual maximum |
| R8 | The whole requested range must be free | Adjacent bookings accepted; partial overlap rejected |
| R9 | Users cancel only their own reservations | Another user's reservation is reported as not found |
| R10 | No cancellation at or after the start time | Before start accepted; exactly at start rejected |
| R11 | A reservation is one continuous interval | Satisfied by construction: a single `tstzrange` |
| R12 | At most 90 days ahead | Exactly 90 days accepted; beyond rejected |
| R13 | A hold not confirmed within its TTL is released | Verified with an injected clock, not by sleeping |
| R14 | A confirmed reservation cannot be double-booked | Enforced by the exclusion constraint, not by application code |

## Where each rule is enforced

**In the domain** (R1–R7, R10, R12) — pure functions over values and an
injected `now`. No I/O, so every boundary is testable without fixtures.

**In the database** (R8, R11, R14) — the exclusion constraint on
`reservations`. R11 is structural: a reservation is one `tstzrange`, so a
caller cannot assemble one out of disjoint pieces.

**In the repository** (R9) — every query is scoped by organization and, where
it matters, by user. A reservation belonging to someone else raises
`ReservationNotFound`, identical to one that does not exist, so the error
cannot be used to probe for other people's bookings.

**In the service** (R13) — a hold whose TTL lapsed is refused at confirmation
and retired immediately, rather than left for the sweeper to find.

## Rules the agent cannot influence

Three things are never derived from model output:

- **Identity.** `user_id` and `org_id` come from the authenticated session and
  are bound by closure. They are not tool parameters, so the model cannot
  choose them.
- **Availability.** Nothing reads availability and then writes. The database
  decides.
- **Consent.** A high blast-radius action is parked until the user agrees, and
  the agreement is matched deterministically rather than by another model call.

## Error responses

Every refusal is actionable. `RoomNotAvailable` carries the conflicting window
and the rooms that *are* free for the whole range, so the agent can offer an
alternative instead of reporting a dead end:

```
Room A is not available for the full time range. It is taken from
2026-09-07T10:00-03:00 to 2026-09-07T11:00-03:00. Rooms free for the
whole range: B, C, D, E.
```
