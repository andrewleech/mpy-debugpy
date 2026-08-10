"""A debuggee that keeps running until something interrupts it.

Every other sample under `src/` reaches a breakpoint by design, which is the
one thing a pause cannot be tested against: a pause has to interrupt a program
that was going to carry on. This one counts instead, bounded so a test that
fails to pause it ends rather than hangs.

`ticks` is a module global on purpose: it advances every iteration, so reading
it twice across a real-time gap is how a test tells a target that is stopped
from one that was merely reported stopped.
"""

import time

ticks = 0

# ~2 s of running at 5 ms an iteration. Long enough that a client has time to
# attach and send a request while the loop is still going, short enough that
# the run-to-completion half of the same test is not a wait.
TICK_LIMIT = 400
TICK_DELAY_MS = 5


def spin(limit=TICK_LIMIT, delay_ms=TICK_DELAY_MS):
    """Advance `ticks` once per `delay_ms`, up to `limit`."""
    global ticks
    ticks = 0
    while ticks < limit:
        ticks += 1
        time.sleep_ms(delay_ms)
    return ticks


def main():
    print("spinner: start")
    total = spin()
    print("spinner: done", total)
