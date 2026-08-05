# MPDBG-READY ordering makes the endpoint unlearnable before attach

Date: 2026-08-05
Top-repo HEAD: 273ac5f9af78e00abc80482445720ce689c97b36
micropython: ddd9cc96c939f8e03487d5e637113cffffbcc58c (`mpremote_debug_command`, s5.1 in review)
micropython-lib: 00d364e7fba71d37ba3923e09a7c8f0a3e2f97a6

Found by the s5.1 adversarial review; verified directly against the tree.

## The defect

`debugpy.listen()` (`python-ecosys/debugpy/debugpy/public_api.py`) does three
things in one call: bind, then `accept()`, then handle the client's
`initialize` request. It returns `(host, port)` only after all three. The
launcher prints the machine-readable handshake line *after* `listen()`
returns (`launcher/mpy_launch_debugpy.py:85` then `:90`).

So `MPDBG-READY {"host":..., "port":..., "caps":...}` is emitted only once a
DAP client has already connected and sent `initialize`. Anything that needs
the endpoint in order to attach cannot get it from the handshake — it would
have to have attached already.

This invalidates the EPIC-5/6 design position "endpoint truth comes from the
device (`MPDBG-READY`), never from user config". `mpremote debug` cannot
learn where to attach, and neither can the transport plumbing s5.4 is meant
to generalise, nor the network flows in EPIC-6.

## Why it went unnoticed

The host harness picks a free port on the *host* (`free_tcp_port` in
`tests/fixtures/debuggee.py`), passes it to the launcher as argv, attaches to
that port, and only then reads the handshake line. The line therefore echoes
back a port the host chose. The suite proves the handshake parses and that
`caps` is honest; it never proves the endpoint is discoverable. A passing
suite was compatible with the design position being decorative.

`--port 0` compounds it: `listen()` resolves the OS-assigned port via
`getsockname()`, but on ports that don't implement it, it substitutes
`DEFAULT_PORT` rather than failing — so a device-assigned port can be
misreported even once ordering is fixed.

## Fix shape

Split bind from accept: `listen()` returns the bound `(host, port)`
immediately after `bind()`/`listen()`, and `accept()` + `initialize`
handling move into `wait_for_client()`. The launcher then prints
`MPDBG-READY` straight after bind, before any client exists, and the
orchestrator can read the endpoint and attach (or forward) to it.

Consequences to handle when implementing:

- `wait_for_client()` currently requires `_debug_session`, which only exists
  after accept; it becomes the call that creates the session, so its
  "no session is listening" early-return changes meaning.
- This aligns the API with CPython debugpy, where `listen()` returns the
  endpoint and `wait_for_client()` blocks — a compatibility gain, not just a
  local fix.
- It is an API-surface change on the `add-debugpy-support` (#1022) lineage,
  so it must land as a commit on that upstream-bound branch, not as a local
  patch.
- The harness should gain coverage that reads the endpoint from the handshake
  *before* connecting, so the design position stays enforced by a test.
- `--port 0` needs either a hard error on ports lacking `getsockname()` or an
  explicit documented default; silently advertising `DEFAULT_PORT` is a
  wrong-endpoint bug once callers actually trust the handshake.

## Effect on STORY-5.1

s5.1's acceptance criterion "prints the parsed endpoint + capability dict
from the `MPDBG-READY` line" is unreachable until the split lands. The
command skeleton itself (registration, argument surface, validation, raw-REPL
upload/exec, handshake reader) is otherwise review-clean. s5.1 is therefore
blocked on a micropython-lib change that was not in its scope, and which no
existing ticket covers.
