const $ = (s) => document.querySelector(s),
  $$ = (s) => [...document.querySelectorAll(s)];
const state = {
  page: "discover",
  rooms: [],
  bookings: [],
  filter: "confirmed",
  editing: null,
  cancelling: null,
  proposal: null,
  user: null,
  requestKey: null,
};
const escapeHTML = (v) =>
  String(v ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
const timezone = Intl.DateTimeFormat().resolvedOptions().timeZone;
const dt = (v) =>
  new Date(v).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
const tm = (v) =>
  new Date(v).toLocaleTimeString(undefined, {
    hour: "numeric",
    minute: "2-digit",
  });
const local = (v) => {
  const d = new Date(v);
  return new Date(d - d.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
};
let toastTimer;
function toast(message) {
  $("#toast").textContent = message;
  $("#toast").classList.remove("hidden");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => $("#toast").classList.add("hidden"), 6000);
}
async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...options.headers },
  });
  let body;
  try {
    body = await response.json();
  } catch {
    throw Error("The server returned an unreadable response.");
  }
  if (!response.ok) {
    if (response.status === 401 && !$("#session-dialog").open)
      $("#session-dialog").showModal();
    const detail = body.detail;
    throw Error(
      typeof detail === "string"
        ? detail
        : Array.isArray(detail)
          ? detail.map((e) => e.msg).join("; ")
          : "Request failed",
    );
  }
  return body;
}
function page(name) {
  state.page = name;
  $$(".page").forEach((el) => el.classList.toggle("hidden", el.id !== name));
  $$(".nav").forEach((el) =>
    el.classList.toggle("active", el.dataset.page === name),
  );
  $("#page-title").textContent = {
    discover: "Find a room",
    bookings: "My bookings",
    assistant: "Huddle assistant",
    reliability: "Booking activity",
  }[name];
  if (name === "discover") search();
  if (name === "bookings") loadBookings();
  if (name === "assistant") loadAssistant();
  if (name === "reliability") loadMetrics();
  history.replaceState(null, "", "#" + name);
}
function queryWindow() {
  const day = $("#search-date").value;
  const start = new Date(day + "T" + $("#search-start").value),
    end = new Date(day + "T" + $("#search-end").value);
  if (!Number.isFinite(+start) || !Number.isFinite(+end) || end <= start)
    throw Error("Choose an end time after the start.");
  return {
    starts_at: start.toISOString(),
    ends_at: end.toISOString(),
    min_capacity: $("#search-people").value,
  };
}
const roomInfo = {
  cedar: {
    sub: "Small group study room.",
    features: ["▧ Whiteboard", "◉ Display", "☀ Daylight"],
    floor: "Ground floor · East wing",
  },
  maple: {
    sub: "Group study room with video-call equipment.",
    features: ["▧ Whiteboard", "◉ Video calls", "☀ Daylight"],
    floor: "First floor · North wing",
  },
  birch: {
    sub: "Large group study room.",
    features: ["◉ Large display", "◎ Conference", "☀ Daylight"],
    floor: "First floor · West wing",
  },
};
function renderRooms(available) {
  $("#room-count").textContent = state.rooms.length;
  $("#rooms").innerHTML = state.rooms
    .map((room) => {
      const info = roomInfo[room.id],
        free = available?.has(room.id);
      return `<article class="room-card"><div class="room-image"><img src="/static/${escapeHTML(room.id)}.svg" alt="Illustration of the ${escapeHTML(room.name)} meeting room"><span class="badge ${free === false ? "pending" : ""}">${free === undefined ? "Explore this space" : free ? "● Available" : "Unavailable for this time"}</span></div><div class="room-body"><div class="room-title"><h3>${escapeHTML(room.name)}</h3><span>♙ &nbsp; Up to ${room.capacity}</span></div><p class="room-description">${info.sub}</p><div class="room-features">${info.features.map((f) => `<span>${f}</span>`).join("")}</div><div class="room-bottom"><span>${info.floor}</span><button data-book="${room.id}" ${free === false ? "disabled" : ""}>Book room</button></div></div></article>`;
    })
    .join("");
}
async function search() {
  try {
    const q = queryWindow(),
      data = await api("/availability?" + new URLSearchParams(q));
    renderRooms(new Set(data.rooms.map((r) => r.id)));
    $("#search-caption").textContent =
      `${data.rooms.length} spaces available · ${dt(q.starts_at)} – ${tm(q.ends_at)}`;
  } catch (e) {
    toast(e.message);
  }
}
async function loadBookings() {
  try {
    state.bookings = await api("/bookings?limit=100");
    $("#booking-count").textContent = state.bookings.filter(
      (b) => b.status === "confirmed",
    ).length;
    renderBookings();
  } catch (e) {
    $("#booking-list").innerHTML =
      `<div class="error-state">${escapeHTML(e.message)}</div>`;
  }
}
function renderBookings() {
  const bookings = state.bookings.filter((b) => b.status === state.filter);
  $("#booking-list").innerHTML = bookings.length
    ? bookings
        .map((b) => {
          const d = new Date(b.starts_at),
            future = d > new Date();
          return `<article class="booking-card"><div class="date-box"><small>${d.toLocaleDateString(undefined, { month: "short" })}</small><b>${d.getDate()}</b></div><div class="booking-content"><h3>${escapeHTML(b.title)}</h3><div class="booking-meta">${escapeHTML(b.room_id[0].toUpperCase() + b.room_id.slice(1))} &nbsp;·&nbsp; ${tm(b.starts_at)} – ${tm(b.ends_at)} &nbsp;·&nbsp; ${b.attendees} people</div><div class="booking-badges"><span class="badge ${b.status === "cancelled" ? "cancelled" : ""}">${b.status === "cancelled" ? "Cancelled" : future ? "Confirmed" : "Past booking"}</span><span class="badge ${b.calendar_status === "synced" ? "neutral" : "pending"}">${{ pending: "Calendar update pending", synced: "Calendar updated", needs_review: "Update needs attention" }[b.calendar_status]}</span></div><div id="events-${b.id}" class="hidden history-inline"></div></div><div class="booking-actions"><button class="button secondary" data-events="${b.id}">Activity</button>${b.calendar_status === "needs_review" ? `<button class="button secondary" data-retry="${b.id}">Retry update</button>` : ""}${b.status === "confirmed" && future ? `<button class="button secondary" data-edit="${b.id}">Edit</button><button class="button secondary" data-cancel="${b.id}">Cancel</button>` : ""}</div></article>`;
        })
        .join("")
    : `<div class="empty-state"><span class="empty-symbol">▦</span><h3>${state.filter === "cancelled" ? "No cancelled bookings" : "No confirmed bookings"}</h3><p>${state.filter === "cancelled" ? "Cancelled bookings will appear here." : "Choose a room and time to create a booking."}</p><button class="button primary" data-page="discover">Explore rooms →</button></div>`;
}
function openBooking(roomId, booking = null) {
  state.editing = booking;
  state.requestKey = crypto.randomUUID();
  $("#dialog-title").textContent = booking
    ? "Edit booking"
    : "Book a room";
  $("#save-booking").textContent = booking
    ? "Save changes →"
    : "Confirm booking →";
  $("#meeting-title").value = booking?.title || "Study session";
  $("#meeting-room").value = booking?.room_id || roomId;
  let q;
  try {
    q = queryWindow();
  } catch {
    return toast("Choose a valid time first.");
  }
  $("#meeting-start").value = local(booking?.starts_at || q.starts_at);
  $("#meeting-end").value = local(booking?.ends_at || q.ends_at);
  $("#meeting-attendees").value =
    booking?.attendees ||
    Math.min(
      Number(q.min_capacity) || 2,
      state.rooms.find((r) => r.id === roomId)?.capacity || 4,
    );
  $("#booking-error").textContent = "";
  $("#booking-dialog").showModal();
}
async function saveBooking(event) {
  event.preventDefault();
  const button = $("#save-booking");
  button.disabled = true;
  try {
    const payload = {
      title: $("#meeting-title").value.trim(),
      room_id: $("#meeting-room").value,
      starts_at: new Date($("#meeting-start").value).toISOString(),
      ends_at: new Date($("#meeting-end").value).toISOString(),
      attendees: Number($("#meeting-attendees").value),
    };
    if (payload.ends_at <= payload.starts_at)
      throw Error("End time must be after start.");
    await api(state.editing ? "/bookings/" + state.editing.id : "/bookings", {
      method: state.editing ? "PUT" : "POST",
      headers: state.editing
        ? { "If-Match": String(state.editing.version) }
        : { "Idempotency-Key": state.requestKey },
      body: JSON.stringify(payload),
    });
    $("#booking-dialog").close();
    toast(
      state.editing
        ? "Booking updated. Previous slot released."
        : "Room booked. You’re all set.",
    );
    await loadBookings();
    page("bookings");
  } catch (e) {
    $("#booking-error").textContent = e.message;
  } finally {
    button.disabled = false;
  }
}
async function cancelBooking() {
  const b = state.cancelling;
  $("#confirm-cancel").disabled = true;
  try {
    await api("/bookings/" + b.id + "/cancel", {
      method: "POST",
      headers: { "If-Match": String(b.version) },
    });
    $("#confirm-dialog").close();
    toast("Booking cancelled. The room is available again.");
    await loadBookings();
  } catch (e) {
    toast(e.message);
  } finally {
    $("#confirm-cancel").disabled = false;
  }
}
const activityLabels = {
  confirmed: ["Booking confirmed", "Your room is reserved."],
  rescheduled: ["Booking changed", "The previous time slot is available again."],
  cancelled: ["Booking cancelled", "The room has been released."],
  calendar_attempt: ["Updating calendar", "Sending the latest reservation details."],
  calendar_synced: ["Calendar updated", "The calendar reflects this reservation."],
  calendar_retry: ["Calendar update delayed", "Your reservation is safe. Another update attempt is scheduled."],
  needs_review: ["Calendar update needs attention", "Your reservation is safe. Open the booking to retry the update."],
  manual_retry: ["Update requested", "A new calendar update has been queued."],
};
const activityText = kind => activityLabels[kind] || ["Booking activity", "Reservation status changed."];
function renderMessages(messages) {
  $("#chat-messages").innerHTML = messages.length
    ? messages
        .map(
          (m) =>
            `<div class="message ${m.role === "user" ? "user" : "assistant"}">${escapeHTML(m.content)}</div>`,
        )
        .join("")
    : '<div class="message assistant">Tell me the date, time, duration, and number of people.</div>';
  $("#chat-messages").scrollTop = $("#chat-messages").scrollHeight;
}
function renderProposal(p) {
  state.proposal = p;
  $("#proposal").innerHTML = p
    ? `<div class="proposal-card"><p class="eyebrow">YOUR APPROVAL REQUIRED</p><h3>${escapeHTML(p.arguments.title)}</h3><p>${escapeHTML(p.arguments.room_id)} · ${p.arguments.attendees} people<br>${dt(p.arguments.starts_at)} – ${tm(p.arguments.ends_at)}<br><small>Expires ${tm(p.expires_at)}. A new message replaces this proposal.</small></p><button class="button primary" id="approve-proposal">Approve & book</button><button class="button secondary" id="dismiss-proposal">Dismiss</button></div>`
    : "";
}
async function loadWorkflow() {
  const {workflow: w} = await api("/assistant/workflow");
  if (!w) {
    $("#workflow-state").textContent = "Start a conversation or a guided demo.";
    return;
  }
  const labels = {pending: "Waiting for your approval", approved: "Reservation confirmed", conflict: "Room taken · choose an alternative", expired: "Proposal expired", superseded: "Replaced by a new request", dismissed: "Proposal dismissed", answered: "Ready for your reply", budget: "Request could not be completed", provider_error: "Assistant unavailable", unavailable: "Room unavailable"};
  $("#workflow-state").innerHTML = `<h3>${escapeHTML(labels[w.status] || w.status)}</h3>
    <p>${w.demo ? "Sample request" : "Assistant request"}</p>
    ${w.status === "conflict" ? `<p>${escapeHTML(w.message)}</p>${w.alternatives.length ? w.alternatives.map(room => `<button class="button secondary" data-alternative="${escapeHTML(room.id)}" data-workflow="${escapeHTML(w.id)}">Review ${escapeHTML(room.name)}</button>`).join("") : "<p>No matching rooms remain. Start a new search.</p>"}` : ""}
    <details class="technical-details"><summary>Demo internals</summary><p>These steps explain how the demo processed this request.</p><ol class="workflow-trace">${w.trace.map(step => `<li>${escapeHTML(step)}</li>`).join("")}</ol><small>Request ${escapeHTML(w.id.slice(0,8))}</small></details>`;
}
async function loadAssistant() {
  try {
    const status = await api("/assistant/status");
    $("#model-status").textContent = status.configured
      ? "Assistant connected"
      : "Chat unavailable · try a sample";
    const h = await api("/assistant/history");
    renderMessages(h.messages);
    renderProposal(h.proposal);
    await loadWorkflow();
  } catch (e) {
    toast(e.message);
  }
}
async function chat(event) {
  event.preventDefault();
  const input = $("#chat-input"),
    message = input.value.trim();
  if (!message) return;
  input.value = "";
  $("#chat-form button").disabled = true;
  renderProposal(null);
  $("#chat-messages").insertAdjacentHTML(
    "beforeend",
    `<div class="message user">${escapeHTML(message)}</div><div class="message assistant loading" id="thinking">Checking your request…</div>`,
  );
  $("#chat-messages").scrollTop = $("#chat-messages").scrollHeight;
  try {
    const result = await api("/assistant/chat", {
      method: "POST",
      body: JSON.stringify({ message, timezone }),
    });
    $("#thinking")?.remove();
    $("#chat-messages").insertAdjacentHTML(
      "beforeend",
      `<div class="message assistant">${escapeHTML(result.message)}</div>`,
    );
    renderProposal(result.proposal);
    await loadWorkflow();
  } catch (e) {
    $("#thinking")?.remove();
    $("#chat-messages").insertAdjacentHTML(
      "beforeend",
      `<div class="message assistant">${escapeHTML(e.message)}</div>`,
    );
  } finally {
    $("#chat-form button").disabled = false;
    $("#chat-messages").scrollTop = $("#chat-messages").scrollHeight;
  }
}
async function loadMetrics() {
  try {
    const m = await api("/metrics");
    $("#worker-status").textContent = m.worker?.online
      ? "Calendar updates available"
      : "Calendar updates paused";
    $("#worker-status").className =
      "badge " + (m.worker?.online ? "" : "pending");
    const cards = [
      ["Confirmed bookings", m.confirmed, "Reserved rooms"],
      [
        "Calendar updated",
        m.synced,
        "Across confirmed & cancelled bookings",
      ],
      ["Waiting for calendar update", m.pending, `${m.needs_review} requiring review`],
      ["Updates needing attention", m.needs_review, "Open the booking to retry"],
    ];
    $("#metrics").innerHTML = cards
      .map(
        ([label, value, note]) =>
          `<div class="metric"><span class="label">${label}</span><strong>${value}</strong><small>${note}</small></div>`,
      )
      .join("");
    $("#sync-diagnostics").textContent = `${m.jobs} calendar tasks; ${m.retried} retried. 95% of completed updates took ${m.sync_p95_seconds === null ? "an unmeasured amount of time" : Number(m.sync_p95_seconds).toFixed(1) + " seconds or less"}. Pending and failed updates are excluded.`;
    $("#activity").innerHTML = m.history.length
      ? m.history.filter(h => h.kind !== "calendar_attempt").slice(0, 12)
          .map(
            (h) =>
              `<div class="activity-item"><span class="event-dot">${h.kind === "calendar_synced" ? "✓" : h.kind.includes("retry") ? "↻" : "·"}</span><div><b>${escapeHTML(h.title)} · ${escapeHTML(activityText(h.kind)[0])}</b><p>${escapeHTML(activityText(h.kind)[1])}</p><small>${dt(h.created_at)} · ${escapeHTML(h.room_id)}</small></div></div>`,
          )
          .join("")
      : '<div class="empty-state"><h3>No activity yet</h3><p>Your booking and calendar updates will appear here.</p></div>';
  } catch (e) {
    toast(e.message);
  }
}
async function actions(event) {
  const target = event.target.closest("button");
  if (!target) return;
  try {
    if (target.dataset.page) page(target.dataset.page);
    if (target.dataset.close) $("#" + target.dataset.close).close();
    if (target.dataset.book) openBooking(target.dataset.book);
    if (target.dataset.filter) {
      state.filter = target.dataset.filter;
      $$(".tab").forEach((t) => t.classList.toggle("active", t === target));
      renderBookings();
    }
    if (target.dataset.edit)
      openBooking(
        null,
        state.bookings.find((b) => b.id === target.dataset.edit),
      );
    if (target.dataset.cancel) {
      state.cancelling = state.bookings.find(
        (b) => b.id === target.dataset.cancel,
      );
      $("#cancel-summary").textContent =
        state.cancelling.title + " · " + dt(state.cancelling.starts_at);
      $("#confirm-dialog").showModal();
    }
    if (target.dataset.events) {
      const el = $("#events-" + target.dataset.events);
      el.classList.toggle("hidden");
      if (!el.classList.contains("hidden")) {
        const events = await api(
          "/bookings/" + target.dataset.events + "/events",
        );
        el.innerHTML = events
          .map(
            (e) =>
              `<p>${escapeHTML(activityText(e.kind)[0])} · ${escapeHTML(activityText(e.kind)[1])}<br><small>${dt(e.created_at)}</small></p>`,
          )
          .join("");
      }
    }
    if (target.dataset.retry) {
      await api("/bookings/" + target.dataset.retry + "/retry-sync", {
        method: "POST",
      });
      toast("Calendar retry queued.");
      loadBookings();
    }
    if (target.classList.contains("suggestion")) {
      $("#chat-input").value = target.textContent;
      $("#chat-input").focus();
    }
    if (target.dataset.demo) {
      target.disabled = true;
      await api("/assistant/demo", {method: "POST", body: JSON.stringify({scenario: target.dataset.demo})});
      await loadAssistant();
      target.disabled = false;
    }
    if (target.dataset.alternative) {
      target.disabled = true;
      await api(`/assistant/proposals/${target.dataset.workflow}/alternative`, {method: "POST", body: JSON.stringify({room_id: target.dataset.alternative})});
      await loadAssistant();
    }
    if (target.id === "refresh-workflow") await loadAssistant();
    if (target.id === "approve-proposal") {
      target.disabled = true;
      await api("/assistant/proposals/" + state.proposal.id + "/approve", {
        method: "POST",
      });
      renderProposal(null);
      toast("Approved. Your room is booked.");
      loadBookings();
      await loadWorkflow();
    }
    if (target.id === "dismiss-proposal") {
      await api("/assistant/proposals/" + state.proposal.id, {
        method: "DELETE",
      });
      renderProposal(null);
      await loadWorkflow();
    }
  } catch (e) {
    target.disabled = false;
    toast(e.message);
    if (state.page === "assistant") await loadAssistant();
  }
}
function setUser(user) {
  state.user = user;
  $("#user-name").textContent = user.name;
  $("#avatar").textContent = user.name[0].toUpperCase();
  $("#greeting").textContent = "Find a study room";
}
async function init() {
  const tomorrow = new Date();
  tomorrow.setDate(tomorrow.getDate() + 1);
  $("#search-date").value = local(tomorrow).slice(0, 10);
  $("#local-time").textContent = new Date().toLocaleDateString(undefined, {
    weekday: "short",
    month: "short",
    day: "numeric",
  });
  $$(".timezone").forEach(
    (e) => (e.textContent = timezone.replaceAll("_", " ")),
  );
  document.addEventListener("click", actions);
  window.addEventListener("hashchange", () => {
    const route = location.hash.slice(1);
    if (["discover", "bookings", "assistant", "reliability"].includes(route))
      page(route);
  });
  $("#search-form").addEventListener("submit", (e) => {
    e.preventDefault();
    search();
  });
  $("#booking-form").addEventListener("submit", saveBooking);
  $("#booking-form").addEventListener(
    "input",
    () => (state.requestKey = crypto.randomUUID()),
  );
  $("#confirm-cancel").addEventListener("click", cancelBooking);
  $("#chat-form").addEventListener("submit", chat);
  $("#ask-assistant").onclick = () => page("assistant");
  $("#profile").onclick = () => $("#profile-dialog").showModal();
  $("#sign-out").onclick = async () => {
    try {
      await api("/session", { method: "DELETE" });
      location.reload();
    } catch (error) {
      toast(error.message);
    }
  };
  $("#reset-chat").onclick = async () => {
    try {
      await api("/assistant/history", { method: "DELETE" });
      renderMessages([]);
      renderProposal(null);
      await loadWorkflow();
    } catch (error) {
      toast(error.message);
    }
  };
  $("#session-form").onsubmit = async (e) => {
    e.preventDefault();
    try {
      const user = await api("/session", {
        method: "POST",
        body: JSON.stringify({ name: $("#display-name").value }),
      });
      setUser(user);
      $("#session-dialog").close();
      await loadBookings();
      if (state.page === "assistant") await loadAssistant();
      if (state.page === "reliability") await loadMetrics();
    } catch (err) {
      toast(err.message);
    }
  };
  try {
    state.rooms = await api("/rooms");
    renderRooms();
    await search();
    setUser(await api("/session"));
    await loadBookings();
  } catch (e) {
    if (!$("#session-dialog").open) toast(e.message);
  }
  const route = location.hash.slice(1);
  if (["discover", "bookings", "assistant", "reliability"].includes(route))
    page(route);
  setInterval(() => {
    if (state.user && state.page === "reliability") loadMetrics();
    if (state.user && state.page === "bookings") loadBookings();
  }, 10000);
}
init();
