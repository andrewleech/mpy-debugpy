# The `stopped` event flake: RESOLVED (root cause below)

Date: 2026-08-06
Top-repo HEAD: 715a669
micropython: e1cfcc83f3c0
micropython-lib: 12ddfbfc96c3

Consolidates what several rounds have established about the harness flake, so
the next investigation starts from the evidence rather than re-deriving it.
The risk register points here.

## What actually happens

A test sets a breakpoint, sends `configurationDone`, and waits for a
`stopped` event. The event does not arrive within the timeout. The captured
stdout of the failing run shows output up to and including the
`configurationDone` response and then nothing — no `stopped` line at all,
ever, not late.

Rate depends on suite size, which is the strongest clue that it is
load-related: reliably green when the suite was ~250 tests, 0 clean runs in 3
at ~280 after STORY-6.3 added firmware-spawning tests. Trimming redundant
sessions recovered most of it (2 clean in 3).

## Ruled out, with measurements

- **Matching strategy is not the cause.** Three independent measurements of
  order-insensitive matching against the recency default: 3/6 vs 4/6
  (2026-08-05), 30/36 vs 27/28 (review, 2026-08-06), 4/8 vs 6/8 (2026-08-06).
  All say scanning is no better or worse, because a stale match satisfies the
  wait early. The positional-indexing hazard that the original diagnosis
  blamed was genuinely removed in `339192a` and `d144e56`, and the flake
  persisted unchanged — so it was at most a minor contributor.
- **It is not a timeout being too tight.** Raising `wait_for_msg`'s default
  from 5s to 20s changed nothing (1 clean in 3, same as before). The event is
  absent, not late.
- **It is not the vacuous assertions.** Those were real (`wait_for_msg`
  returned true unconditionally, nine call sites) and are fixed, but the
  flake predates and outlives that fix.

## The question that has not been asked

Nobody has yet determined whether the `stopped` event is **never emitted by
the debuggee** or **emitted and never received by the harness**. Every round
so far has worked on the receiving side. The two have completely different
fixes, and distinguishing them is the first thing to do.

Concretely: instrument the device side (`debug_session.py`'s trace callback
and wherever the `stopped` event is sent) to log to a file, not the socket,
then run the suite until it fails and read that log. If the device logged a
send, the loss is in the socket/harness path; if it never logged, the
breakpoint did not fire on the device at all and the problem is in settrace
under repeated fresh interpreters.

## Other things worth checking, in rough order

1. **Is it the first test after a burst?** Record which test index within a
   run fails; if failures cluster after many spawns, resource exhaustion (fds,
   ports in TIME_WAIT, thread count) is implicated.
2. **Is it WSL-specific?** The suspicion was raised but never tested. This
   machine is WSL2; a run on native Linux or in CI would settle it. Note CI
   has never shown this on the four-attempt retry, which weakly suggests it is
   environment-sensitive.
3. **Port reuse.** Sessions bind ephemeral ports in quick succession, and the
   default-port path binds the same port repeatedly. A device still holding a
   port from the previous test would make the next session's client attach to
   a corpse. `ss -ltnp` at failure time would show it.
4. **The `configurationDone` handshake.** `wait_for_client()` returns on
   `configurationDone`; the target then imports and runs. If the import is
   slow under load the breakpoint may be set before the module exists. Timing
   the gap between `configurationDone` and the first line event would show it.

## What not to do

Do not change how `wait_for_msg` matches again. Three measurements have ruled
it out, and each attempt has cost a round. Do not widen the CI allowlist
further as a substitute for diagnosis — it is already covering ids it was not
written for, which is what prompted escalating this.


## Resolved (2026-08-06, commit 3414f0a)

**It was a product deadlock, not a harness defect.** `process_pending_messages()`
set a 1 ms socket timeout and restored blocking mode in its `finally`. The
trace function calls it on entry to every new frame, so handling a message
re-enters it, and the inner call's `finally` put the socket back into
blocking mode underneath the outer pump. That pump's next `recv()` then
waited for a message the client will not send until it has seen an event the
pump itself is what produces — the two sides deadlock.

The answer to the question this note was written around: the device logged
NEITHER detect nor send, because it never reached the target at all. It
parked in `wait_for_client()`, so settrace was never armed and no `stopped`
event was ever constructed. Every earlier round worked the receiving side,
which was innocent throughout.

Fixed by tracking the nesting (MicroPython sockets have no `gettimeout()` to
save and restore) and returning immediately from a re-entrant call — the
outer loop is already draining.

Measured: worst-affected file 4 clean in 6 -> 6 in 6; full suite 0 clean in 3
-> 3 in 4.

Two things worth keeping:

- The same defect was degrading throughput even when it did not deadlock:
  ~100 ms per traced statement, spent in blocking reads.
- Instrumentation on this path is Heisenberg-sensitive. A diagnostic call per
  pump iteration made every test in the file fail, because each extra frame
  is itself traced and drives another nested pump. Any future probe must stay
  off the traced path.
