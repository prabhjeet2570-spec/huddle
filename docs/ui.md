# Interface decisions

The room directory starts with date, time, group size, and a floor filter. Room
numbers identify spaces across four floors. Warm paper backgrounds, rust-colored controls, and compact illustrated room
listings give the directory its own character. A horizontal navigation bar replaces
the dashboard sidebar; clear sans-serif headings keep the booking form practical.
Local room illustrations describe fictional spaces rather than claim photographs
of a real library.

The unclickable Workspace breadcrumb, “Three sample rooms” subtitle, separate
assistant navigation page, calendar explainer panel and latency diagnostics were
removed. A speech-bubble launcher opens the booking assistant beside discovery;
closing it preserves the conversation. The legacy `#assistant` URL opens this drawer
on Find a room. Mobile uses an inset drawer with independent scrolling.

After approval, a server-generated assistant message names the room and group
size. That message is stored in conversation history, so refresh shows the same
confirmation. Repeating approval returns the same booking and does not add another
confirmation. Room conflicts still require a fresh proposal and approval.

Reservation status is the primary booking signal. Calendar status and Retry update
live in a collapsed **Technical demo details** disclosure on each booking. Open
disclosures remain open across background refreshes. The main activity page shows
only booked, changed and cancelled events; a separate collapsed section contains
worker status, calendar counts and processing history. Historical events are clearly
labeled as history rather than current booking state. The technology description
and simulator limitations live in the README.
The completed-only p95 is available through the API, not displayed as a product
performance claim. Technical workflow traces are optional and collapsed.

A local-only profile selector exposes the populated sample sessions. Upcoming,
past and cancelled reservations have separate tabs. Room-size mismatches are
labeled “Too small for this group”; occupied spaces say “Booked for this time.”
Floor filters also update the count of available rooms.

Discovery and the chat drawer were checked at 390px with no horizontal overflow.
Desktop discovery, approval, booking tabs and profile switching were checked at
1440px. Screenshots show the running application and synthetic sample data.
