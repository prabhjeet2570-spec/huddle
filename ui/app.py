"""Streamlit client.

Deliberately thin. It holds a bearer token and a conversation id, and nothing
else: history, budgets and confirmation state all live on the server, so the
client cannot forge a transcript or talk the agent past a guardrail.
"""

from __future__ import annotations

import json
import os
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import streamlit as st

API_BASE_URL = os.getenv(
    "HUDDLE_API_BASE_URL", os.getenv("API_BASE_URL", "http://localhost:8000")
).rstrip("/")
TIMEOUT_SECONDS = 120


class ApiError(Exception):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        self.status_code = status_code
        super().__init__(message)


def main() -> None:
    st.set_page_config(page_title="Huddle", page_icon="📅", layout="centered")
    _init_state()

    if not st.session_state.token:
        _login_view()
        return

    _chat_view()


def _init_state() -> None:
    for key, default in (
        ("token", None),
        ("messages", []),
        ("conversation_id", None),
        ("username", ""),
        ("organization", ""),
        ("awaiting_confirmation", False),
    ):
        if key not in st.session_state:
            st.session_state[key] = default


def _login_view() -> None:
    st.title("Huddle")
    st.caption("Conversational meeting-room booking")

    with st.form("login"):
        organization = st.text_input("Organization", value="acme")
        username = st.text_input("Username", value="alice")
        password = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Sign in")

    if not submitted:
        return

    if not all((organization, username, password)):
        st.error("Fill in every field.")
        return

    try:
        with st.spinner("Signing in..."):
            response = _post(
                "/auth/login",
                {
                    "organization": organization,
                    "username": username,
                    "password": password,
                },
            )
    except ApiError as error:
        st.error(
            "Wrong organization, username or password."
            if error.status_code == 401
            else str(error)
        )
        return

    st.session_state.token = response["access_token"]
    st.session_state.username = username
    st.session_state.organization = organization
    st.session_state.messages = []
    st.session_state.conversation_id = None
    st.rerun()


def _chat_view() -> None:
    with st.sidebar:
        st.subheader("Session")
        st.write(f"**{st.session_state.username}** @ {st.session_state.organization}")
        conversation = st.session_state.conversation_id
        st.caption(f"Conversation: {conversation or 'not started'}")

        if st.button("New conversation", use_container_width=True):
            st.session_state.conversation_id = None
            st.session_state.messages = []
            st.session_state.awaiting_confirmation = False
            st.rerun()

        if st.button("Sign out", use_container_width=True):
            st.session_state.clear()
            st.rerun()

        st.divider()
        _render_bookings()

    st.title("Huddle")

    for message in st.session_state.messages:
        with st.chat_message(message["role"]):
            st.markdown(message["content"])
            if message.get("caption"):
                st.caption(message["caption"])

    if st.session_state.awaiting_confirmation:
        st.info(
            "This action changes a real booking, so it needs your explicit "
            "confirmation. Reply **yes** to go ahead or **no** to stop."
        )

    prompt = st.chat_input("Ask for a room, or confirm a pending booking")
    if not prompt:
        return

    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    with st.chat_message("assistant"):
        try:
            with st.spinner("Thinking..."):
                payload: dict[str, Any] = {"message": prompt}
                if st.session_state.conversation_id:
                    payload["conversation_id"] = st.session_state.conversation_id
                response = _post("/chat", payload, token=st.session_state.token)
        except ApiError as error:
            st.error(str(error))
            st.session_state.messages.pop()
            return

        reply = response["response"]
        st.markdown(reply)

        caption = (
            f"{response['tool_calls']} tool call(s) · "
            f"{response['latency_ms']:.0f} ms · {response['status']}"
        )
        st.caption(caption)

        if response["status"] == "escalated":
            st.warning("This conversation was flagged for human review.")

    st.session_state.conversation_id = response["conversation_id"]
    st.session_state.awaiting_confirmation = response["awaiting_confirmation"]
    st.session_state.messages.append(
        {"role": "assistant", "content": reply, "caption": caption}
    )
    st.rerun()


def _render_bookings() -> None:
    st.subheader("Your bookings")
    try:
        bookings = _get("/bookings/me", token=st.session_state.token)
    except ApiError as error:
        st.caption(f"Unavailable: {error}")
        return

    if not bookings:
        st.caption("Nothing booked yet.")
        return

    for booking in bookings:
        badge = "HOLD" if booking["state"] == "held" else "BOOKED"
        st.markdown(
            f"**{booking['reference']}** · `{badge}`  \n"
            f"Room {booking['room']} — {booking['title']}  \n"
            f"{booking['starts_at'][:16].replace('T', ' ')} to "
            f"{booking['ends_at'][11:16]}"
        )


def _request(
    path: str,
    method: str,
    payload: dict[str, Any] | None = None,
    token: str | None = None,
) -> Any:
    headers = {"Accept": "application/json"}
    body = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(payload).encode("utf-8")
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"

    request = Request(
        url=f"{API_BASE_URL}{path}", data=body, headers=headers, method=method
    )
    try:
        with urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        raise ApiError(_error_message(error), status_code=error.code) from error
    except (URLError, TimeoutError) as error:
        raise ApiError(
            f"Could not reach the API at {API_BASE_URL}. Is it running?"
        ) from error


def _post(path: str, payload: dict[str, Any], token: str | None = None) -> Any:
    return _request(path, "POST", payload, token)


def _get(path: str, token: str | None = None) -> Any:
    return _request(path, "GET", None, token)


def _error_message(error: HTTPError) -> str:
    try:
        detail = json.loads(error.read().decode("utf-8")).get("detail")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return f"The API returned an error ({error.code})."
    if isinstance(detail, str):
        return detail
    return f"The API returned an error ({error.code})."


if __name__ == "__main__":
    main()
