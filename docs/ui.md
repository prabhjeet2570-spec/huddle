# Interface decisions

Huddle is a local study-room booking demo. The interface prioritizes room search,
reservation details, approval, and booking activity. Architecture belongs in the
README and architecture document rather than the main booking flow.

The discovery page starts with time/capacity search. The marketing hero, promotional
slogans, technology footer, and database-enforcement explanations were removed.
The palette uses neutral surfaces and a restrained slate accent; card borders,
compact room illustrations, and consistent controls replace decorative effects.

“Booking activity” reports user-visible outcomes. Calendar update status is useful
because a reservation can be confirmed while its calendar update is delayed, and
an exhausted update needs an explicit retry. Raw queue/revision terminology is
translated to readable activity descriptions. Routine attempt events are omitted
from the summary, with detailed booking history still available.

Two disclosures retain technical information for reviewers who choose it:

- **Demo internals:** the saved agent steps help explain and inspect LangGraph.
- **Calendar diagnostics:** completed-update latency and retries help investigate
  synchronization behavior. The text explains that pending/failed work is excluded.

These details start collapsed. The user-facing request state and alternative-room
actions remain visible. The demo/simulated-calendar labels stay visible because
Huddle does not reserve a real room or update a personal calendar.

Mobile room discovery and assistant approval were checked at 390px; desktop
room discovery was checked at 1280px. Both fit without horizontal overflow.
