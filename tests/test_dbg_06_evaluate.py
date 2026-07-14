"""Behavioural tests for STORY-6.5: DAP `evaluate` statement exec.

Drives real DAP `evaluate` requests with explicit `context` fields against
the running MicroPython debuggee (never source-string checks), covering:

- `repl`/`clipboard`: a statement that eval() rejects with SyntaxError falls
  back to exec() against the frame's live globals, so a global assignment
  actually mutates the running program (observed after `continue`) and a
  `def` rebinds a name for a later call.
- `watch`/`hover` (and the context-absent default): unchanged, eval()-only
  behaviour - a statement is still rejected exactly as before this feature.
- The honesty rule: assigning a name shadowed by a paused frame's LOCAL
  yields a visible warning, and the local itself is provably unchanged.
- A statement that raises during exec() surfaces as `success=False` with
  the exception message, and the session remains usable afterwards.
"""

from pathlib import Path
from typing import List

import pytest
from helpers import set_breakpoints, wait_for_msg

_REPO_ROOT = Path(__file__).resolve().parents[1]
_TARGET_PY = str(_REPO_ROOT / "src" / "target.py")


def _last_evaluate_body(server):
    msg = server.rcv_messages[-1]
    assert msg.type == "response" and msg.command == "evaluate", msg
    return msg


@pytest.mark.parametrize("source_file, bp_lines", [(_TARGET_PY, [104])], indirect=True)
@pytest.mark.parametrize("context", ["watch", "hover"])
def test_evaluate_watch_hover_reject_statements(
    attach_server, source_file: str, bp_lines: List[int], micropython_debuggee, context: str
):
    """Regression: `watch`/`hover` never exec a statement, only eval()."""
    server = attach_server
    client = server.client

    set_breakpoints(server, source_file, bp_lines)
    wait_for_msg(server, response="setBreakpoints")
    client.configuration_done()
    assert wait_for_msg(server, event="stopped")

    # A plain expression still evaluates normally in either context.
    server.clear_messages()
    client.evaluate("1 + 1", frame_id=0, context=context)
    wait_for_msg(server, response="evaluate")
    msg = _last_evaluate_body(server)
    assert msg.success is True
    assert msg.body["result"] == "2"

    # A statement is a SyntaxError under eval() and is rejected, not executed.
    server.clear_messages()
    client.evaluate("numbers = [1, 2, 3]", frame_id=0, context=context)
    wait_for_msg(server, response="evaluate")
    msg = _last_evaluate_body(server)
    assert msg.success is False, f"{context} context executed a statement: {msg.raw}"
    assert "Evaluation error" in (msg.message or "")


@pytest.mark.parametrize("source_file, bp_lines", [(_TARGET_PY, [104])], indirect=True)
def test_evaluate_default_context_rejects_statements(
    attach_server, source_file: str, bp_lines: List[int], micropython_debuggee
):
    """No `context` field at all defaults to `watch` semantics (unchanged)."""
    server = attach_server
    client = server.client

    set_breakpoints(server, source_file, bp_lines)
    wait_for_msg(server, response="setBreakpoints")
    client.configuration_done()
    assert wait_for_msg(server, event="stopped")

    server.clear_messages()
    client.send_request("evaluate", {"expression": "numbers = [1, 2, 3]", "frameId": 0})
    wait_for_msg(server, response="evaluate")
    msg = _last_evaluate_body(server)
    assert msg.success is False, f"context-less evaluate executed a statement: {msg.raw}"


@pytest.mark.parametrize("source_file, bp_lines", [(_TARGET_PY, [104, 106])], indirect=True)
def test_evaluate_repl_statement_mutates_running_global(
    attach_server, source_file: str, bp_lines: List[int], micropython_debuggee
):
    """`x = 42`-style global assignment via repl takes effect on `continue`.

    Breaks in `mathematics()` before `foo += result` runs, overwrites the
    module global `foo` via a repl-context `evaluate`, then continues to a
    second breakpoint placed after `foo += result` and confirms the running
    program's own arithmetic used the overwritten value (not the original).
    """
    server = attach_server
    client = server.client

    set_breakpoints(server, source_file, bp_lines)
    wait_for_msg(server, response="setBreakpoints")
    client.configuration_done()
    assert wait_for_msg(server, event="stopped")

    server.clear_messages()
    client.evaluate("foo = 12345", frame_id=0, context="repl")
    wait_for_msg(server, response="evaluate")
    msg = _last_evaluate_body(server)
    assert msg.success is True, msg.raw
    assert msg.body["result"] == "", f"plain statement should return empty result: {msg.raw}"

    # Confirm the assignment is visible immediately, before continuing.
    server.clear_messages()
    client.evaluate("foo", frame_id=0, context="watch")
    wait_for_msg(server, response="evaluate")
    assert _last_evaluate_body(server).body["result"] == "12345"

    # Continue past `foo += result` (fibonacci(3) == 2) to the next breakpoint.
    server.clear_messages()
    client.continue_(thread_id=1)
    assert wait_for_msg(server, event="stopped"), "did not reach the second breakpoint"

    server.clear_messages()
    client.evaluate("foo", frame_id=0, context="watch")
    wait_for_msg(server, response="evaluate")
    result = _last_evaluate_body(server).body["result"]
    assert result == "12347", (
        f"running program did not observe the repl-assigned global: foo={result}"
    )


@pytest.mark.parametrize("source_file, bp_lines", [(_TARGET_PY, [104])], indirect=True)
def test_evaluate_repl_def_statement_rebinds_for_later_call(
    attach_server, source_file: str, bp_lines: List[int], micropython_debuggee
):
    """`def f(): return 1` via repl defines a callable usable in a later evaluate."""
    server = attach_server
    client = server.client

    set_breakpoints(server, source_file, bp_lines)
    wait_for_msg(server, response="setBreakpoints")
    client.configuration_done()
    assert wait_for_msg(server, event="stopped")

    server.clear_messages()
    client.evaluate("def f():\n    return 1", frame_id=0, context="repl")
    wait_for_msg(server, response="evaluate")
    msg = _last_evaluate_body(server)
    assert msg.success is True, msg.raw

    server.clear_messages()
    client.evaluate("f()", frame_id=0, context="repl")
    wait_for_msg(server, response="evaluate")
    msg = _last_evaluate_body(server)
    assert msg.success is True, msg.raw
    assert msg.body["result"] == "1"


@pytest.mark.parametrize("source_file, bp_lines", [(_TARGET_PY, [80])], indirect=True)
def test_evaluate_repl_shadowed_local_warns_and_leaves_local_unchanged(
    attach_server, source_file: str, bp_lines: List[int], micropython_debuggee
):
    """Adversarial honesty-rule case: assigning a name shadowed by a frame LOCAL.

    Breaks in `main()` at the `for` header (line 80), where local `x == 78`
    is already bound (see test_epic1_foundations.py for why this line, not
    79). Assigning `x` via repl lands in globals only; the response must say
    so, and re-reading `x` in the same frame must still show the untouched
    local.
    """
    server = attach_server
    client = server.client

    set_breakpoints(server, source_file, bp_lines)
    wait_for_msg(server, response="setBreakpoints")
    client.configuration_done()
    assert wait_for_msg(server, event="stopped")

    server.clear_messages()
    client.evaluate("x = 999", frame_id=0, context="repl")
    wait_for_msg(server, response="evaluate")
    msg = _last_evaluate_body(server)
    assert msg.success is True, msg.raw
    result = msg.body["result"]
    assert "local" in result.lower() and "unchanged" in result.lower(), (
        f"expected a shadowed-local warning, got: {result!r}"
    )

    # The paused frame's local `x` must still read 78, not 999.
    server.clear_messages()
    client.evaluate("x", frame_id=0, context="watch")
    wait_for_msg(server, response="evaluate")
    assert _last_evaluate_body(server).body["result"] == "78", (
        "local 'x' changed even though the honesty rule promises it can't"
    )


@pytest.mark.parametrize("source_file, bp_lines", [(_TARGET_PY, [104])], indirect=True)
def test_evaluate_repl_statement_exception_keeps_session_usable(
    attach_server, source_file: str, bp_lines: List[int], micropython_debuggee
):
    """A statement that raises during exec() is success=False; session survives."""
    server = attach_server
    client = server.client

    set_breakpoints(server, source_file, bp_lines)
    wait_for_msg(server, response="setBreakpoints")
    client.configuration_done()
    assert wait_for_msg(server, event="stopped")

    server.clear_messages()
    client.evaluate("raise ValueError('boom')", frame_id=0, context="repl")
    wait_for_msg(server, response="evaluate")
    msg = _last_evaluate_body(server)
    assert msg.success is False, f"expected the raised exception to fail the request: {msg.raw}"
    assert "boom" in (msg.message or "")

    # The session must still respond to further requests.
    server.clear_messages()
    client.evaluate("1 + 1", frame_id=0, context="repl")
    wait_for_msg(server, response="evaluate")
    msg = _last_evaluate_body(server)
    assert msg.success is True, f"session did not survive the failed statement: {msg.raw}"
    assert msg.body["result"] == "2"
