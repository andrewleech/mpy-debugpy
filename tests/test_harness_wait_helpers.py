"""Contract tests for the harness's own waiting helpers.

Every other test in the suite uses these as instruments and so cannot check
them: a helper that reports success without having found anything makes the
tests built on it green rather than failing itself. What is pinned here is that
each way of asking either returns what it matched or reports nothing at all.
"""

import pytest
from dap import Message
from helpers import set_breakpoints, take_msg, wait_for_msg


class FakeClient:
    """Records what a helper asked the client to send."""

    def __init__(self):
        self.sent = []

    def set_breakpoints(self, **kwargs):
        self.sent.append(kwargs)


class FakeServer:
    """A message source standing in for the two ways messages arrive.

    `wait_for_msg` pumps the socket itself, one `run_single` per poll, so
    `pending` drips through that call and a timeout stays reachable.
    `take_msg` never polls - it reads what the server's background reader
    thread has already appended - so `arrived` is placed straight into
    `rcv_messages`.
    """

    def __init__(self, pending=(), arrived=()):
        self.pending = list(pending)
        self.rcv_messages = list(arrived)
        self.cursor = 0
        self.polls = 0
        self.client = FakeClient()

    def run_single(self):
        self.polls += 1
        if self.pending:
            self.rcv_messages.append(self.pending.pop(0))


def _event(name):
    return Message({"type": "event", "event": name})


def _response(command):
    return Message({"type": "response", "command": command})


def test_a_wait_with_no_criterion_is_an_error():
    with pytest.raises(ValueError):
        wait_for_msg(FakeServer(), timeout=0.1)


def test_a_matched_event_comes_back_as_the_message():
    server = FakeServer(pending=[_event("stopped")])

    matched = wait_for_msg(server, event="stopped", timeout=1)

    assert matched is not None
    assert matched.event == "stopped"


def test_a_message_that_never_arrives_is_not_success():
    server = FakeServer()

    assert wait_for_msg(server, event="stopped", timeout=0.3) is None


def test_the_wrong_message_is_not_success():
    """The awaited message must be the most recent one, so this is a miss."""
    server = FakeServer(pending=[_response("setBreakpoints")])

    assert wait_for_msg(server, event="stopped", timeout=0.3) is None


def test_take_msg_walks_a_cursor_instead_of_reading_the_last_message():
    server = FakeServer(arrived=[_event("stopped"), _response("continue"), _event("stopped")])

    first = take_msg(server, event="stopped", timeout=1)
    second = take_msg(server, event="stopped", timeout=1)

    assert first is not None and second is not None
    assert first is not second


def test_take_msg_needs_a_criterion_too():
    with pytest.raises(ValueError):
        take_msg(FakeServer(), timeout=0.1)


def test_a_fire_and_forget_breakpoint_set_has_nothing_to_assert():
    """The request goes out, but an unwaited call reports no confirmation."""
    server = FakeServer(pending=[_response("setBreakpoints")])

    assert set_breakpoints(server, "src/app.py", [3]) is None
    assert server.client.sent


def test_a_waited_breakpoint_set_returns_the_response():
    server = FakeServer(pending=[_response("setBreakpoints")])

    assert set_breakpoints(server, "src/app.py", [3], wait=True) is not None
