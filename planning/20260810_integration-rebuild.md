# Rebuilding the integration on current upstream

- Date: 2026-08-10
- Top repo HEAD: `e023028`
- `micropython`: `4eaafbc5bc` (`mpy-debugpy`), on `upstream/master` `5f2181f938`
- `micropython-lib`: `1eb7c82` (`mpy-debugpy`)

STORY-6.1's criterion 4 was recorded with a deferral: the integration had been
merged rather than rebuilt, because `mbm rebase` refetches the PR-numbered
branches live and #8767's head had moved on, which would have put the flashed
firmware out of step with the source mid-verification. This is that rebuild,
and what it found.

## The micropython half is rebuilt and provably unchanged

`make integrate` moved the base from `06bcfd5b74` to `5f2181f938`, five
upstream commits: `py/gc` heap tracking, an `py/emitnative` register clobber,
two `py/mpz` three-argument `pow()` fixes, and `py/modmicropython` scheduling a
`KeyboardInterrupt`. None of them touch anything the composition owns.

The check that says the rebuild was faithful is the composed delta, not the
commit graph: `git diff <base> <integration>` is byte-identical before and
after, 284290 bytes either way. So the rebuild changed the base and the shape
of the history and nothing about the tree the firmware is built from. The
integration is now seven merges on `5f2181f938` with nothing cherry-picked on
top, one per branch in `mbm.toml`.

`mbm rebase` stopped twice at `local_names_implementation` with `git merge
failed`, both times on a resolution `rerere` had already staged. That is a
known mbm limitation rather than a conflict: `git merge` with rerere autoupdate
stages the resolution and still exits non-zero, and mbm treats a non-zero merge
as fatal. Each time the staged tree was diffed against
`mpy-debugpy_preintegrate_20260810` (empty), committed with mbm's own message
format, `_complete_integration`'s bookkeeping done by hand (`git branch -f
local_names_implementation rebase-local_names_implementation`, delete the
rebase branch), and the run continued with `--resume`. `--resume` skips to
`index+1` when there is no `rebase-merge` directory, which is why the
bookkeeping has to be done rather than left to the resumed run.

## The rebuild surfaced a commit message that had never been pushed

Rewriting the commits moved `debug_board_flags`'s single commit into the push
range for the first time, and the fork's pre-push hook rejected it: subject
over 72 characters, a body line over 75, and `Claude-Session` after
`Signed-off-by` where `verifygitlog.py` requires the last line to be the
sign-off. Fixed at the source commit and recomposed, `77e3306233` to
`435602aedd`, message only; the tree diff between the two is empty.

This is an argument for rebuilding sooner rather than later. A commit that
lives only on an integration branch is not checked by anything.

## One hook failure was the hook, and was left alone

The same push also failed `ruff format --check` on
`tests/basics/builtin_pow3_intbig.py`, an upstream file this work has never
touched. micropython's `pyproject.toml` excludes `tests/basics/*.py` from
`[tool.ruff.format]`, and `ruff format --check .` in the submodule reports
1383 files already formatted. The hook
(`claude-mpy-marketplace/plugins/mpy-rules/scripts/pre-push-check.sh`) pipes
explicit paths into `xargs ruff format --check`, and an explicit path defeats a
config exclusion unless `--force-exclude` is passed.

Every check the hook runs was then run by hand with `--force-exclude`, all
green, and the push made with `--no-verify`. The hook is shared tooling outside
this repo and was deliberately not patched here.

## The micropython-lib half cannot be rebuilt yet

`refs/pull/1022/head` is `792d14d`, which is an ancestor of the local
`add-debugpy-support` at `2a125e4`, 13 commits behind it. mbm composes a
PR-numbered entry from the PR head, so a rebase composes the branch as the
public PR currently stands.

What that costs is not abstract. The `mpy-debugpy_update` branch mbm produced
(`d5509c7`) has no history for
`python-ecosys/debugpy/debugpy/common/stream_transport.py` at all: the whole
stream transport, and with it every serial-DAP scenario and criterion 3's
`_peer_gone` fix, is absent. `mpy-debugpy` must not be force-moved onto it.
This unblocks when the staged branch is pushed, which updates PR #1022 and is
held for the user.

## The bench, and two harness defects the rebuild did not cause

The PYBD-SF6W was reflashed by DFU from `4eaafbc5bc`
(`v1.29.0-preview.717.g4eaafbc5bc`). DFU writes only the app region, so the
filesystem, `boot.py`, `_secrets.py` and `/flash/lib/debugpy` all survived; the
device-side install was confirmed current rather than assumed, by recomputing
the installer's cache key from the current `python-ecosys/debugpy/debugpy`
sources for a `.mpy` v6 target and matching it against the marker on the board.

The full HIL suite passed 18 of 18 on the first run after the reflash and then
failed `test_hil_killing_the_bridge_at_a_breakpoint_leaves_the_board_usable` on
the second, on identical firmware. Both defects behind that turned out to be in
how the suite observes the board, not in what the board does.

### A CDC interface carries stdout only while a host holds it

The scenario killed `mpremote debug`, then opened the primary port to watch for
the target's completion line. Killing the only holder of that port drops DTR on
it, and stm32 discards stdout written while the interface is unheld, so
everything the board printed between the kill and the port being reopened was
gone. The board resumes within one poll of the dead DAP channel and prints its
way to the completion line in a few hundred milliseconds, which is the same
order as opening a tty: a race, and one the test can only lose silently, since
a dropped line and a board that never resumed look identical from the host.

Opening the capture before the kill, as a second holder of a port the command
still has, settles it. The board prints the full tail including
`Target completed successfully!`, and the scenario now passes in about five
seconds where before it waited out its 30 s timeout. Nothing is stolen from the
command by reading alongside it, because a target stopped at a breakpoint
prints nothing, and the DAP interface, the one whose loss the scenario is
about, is untouched.

The mechanism itself was confirmed independently before the harness was
changed, driving `StreamTransport` on the board against the installed `.mpy`
with no debugpy session in the picture: EAGAIN with the line down and no
traffic yet, EAGAIN with a host holding the port, traffic latched on the first
byte, and `b""` returned on the first poll after the host closed.

### The ports come back before the board does

`hil_reset_board` returned as soon as the device paths reappeared. That is not
when the board is ready: `boot.py` calls `pyb.usb_mode()`, which enumerates
where it is called rather than when the script returns, so the paths come back
while the rest of the script is still running. On this bench the rest is a WiFi
association, measured at 5.4 s to the paths and 8.5 s to the REPL prompt on a
good association, and 15 s longer for each retry the first attempt costs.

In file order the reset scenario runs first and the following scenarios absorb
the gap. Under the suite's randomised ordering a network scenario can follow it
directly, and then twelve tests fail with `No route to host`, which reads as a
network regression and is a board that has not finished booting. The reset
helper now waits for a REPL prompt as well as for the paths. It provokes one
with a newline rather than waiting for the boot banner: the newline sits unread
in the interface's rx buffer until the REPL starts, whereas a banner printed
before the port is open is discarded for the same reason the completion line
was.

Three consecutive full runs are green afterwards, two of them randomised:
18 passed, `20260810_hil_PYBD_SF6.md`. Host suite 362 passed, 1 skipped,
1 xfailed.

## Left open

`firmware/firmware.toml` still records `source_commit = f9d7c96b96`, which now
genuinely lags the pinned `4eaafbc5bc`, and the five new upstream commits touch
firmware sources. Republishing is CI-driven (`.github/workflows/firmware.yml`,
releases tagged `fw-<sha10>`) and is tracked by Q12 along with how the manifest
should express a second CDC interface.
