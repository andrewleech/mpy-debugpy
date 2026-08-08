"""What gets debugged on the board, and how it names itself there.

Shared by the fixtures that install it and the tests that set breakpoints in
it. Kept out of `conftest.py` so tests can import it by name: there is a
`conftest` module at two levels of this suite, and which one `import conftest`
resolves to depends on sys.path order.
"""

from pathlib import Path

_TOP_DIR = Path(__file__).resolve().parents[2]

# The same debuggee the unix suites use, so the two differ only in runtime.
TARGET_SRC = _TOP_DIR / "src" / "target.py"
TARGET_MODULE = "target"

# A board reports the name an import resolved, not an absolute path, so this
# is what appears in `source.path` and what a breakpoint has to be set on.
DEVICE_SOURCE_PATH = f"{TARGET_MODULE}.py"

# Two lines in that file. `LOOP_LINE` is reached almost immediately; at
# `LOCALS_LINE` roughly two dozen locals are bound, one a 120-element list.
LOOP_LINE = 80  # `for _ in range(3):` in main()
LOCALS_LINE = 73  # `return spanish_inquisition` in inspect_local_variables()

# `LOOP_LINE` is the loop *header*, so it is reached once per pass and a
# breakpoint on it fires on every one of them.
LOOP_ITERATIONS = 3
LOOP_BODY_MARK = "Done..."  # last line of the loop body, one print per pass

# `main()` returning is not visible over DAP - this server emits no terminated
# or exited event - so the launcher's own line is the evidence that the target
# ran to the end rather than dying under the tracer.
COMPLETION_LINE = "Target completed successfully!"
