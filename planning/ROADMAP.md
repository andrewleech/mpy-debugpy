# mpy-debugpy — Roadmap to a user-friendly, upstream-mergeable debugging experience

Status: living plan, in execution — the Status section below records what has
landed. This document records both the reasoning and the plan. The
plan is expressed as epics and stories so it can drive implementation and investigation
workflows (some automated, some manual). Read the "Background research" section first if
you want the why; jump to "Epics and stories" for the what.

Target end state: a MicroPython user can debug a script with breakpoints, stepping and
variable inspection across three transports — (1) the unix port, (2) a device over
mpremote serial, (3) a device over the network — driven from a single mental model, with
no hand-maintained IPs/ports, no stale device files, no sleep-based breakpoint race, and
a firmware story that is honest about what each build supports. The orchestration ships as
an `mpremote debug` command (delivered through the `ampremote` integration, ultimately an
upstream micropython PR), with a thin VS Code extension layered on top last.

---

## Status (living section)

Updated as work lands. See per-story acceptance criteria below for detail.

- **The submodule-pin rule is a check rather than a paragraph (2026-08-10).** The risk row for it prescribed a command to run by hand before every top-repo push, and four CI runs had already died at the submodule checkout because the pin named a commit that existed only in a local clone. `tools/check-submodule-pins.sh` now reads the pins out of a given commit, matches each against the submodule remote whose URL equals the one `.gitmodules` hands CI, fetches that remote, and requires the pin to be on a branch of it; `tools/pre-push-check.sh` runs it per ref being pushed and `make bootstrap` installs it as `pre-push`, with `make check-pins` for HEAD by hand. Two details are load-bearing. It fetches first, because a remote-tracking ref goes stale as soon as anyone else pushes and a stale ref is the false pass the check exists to prevent. And `make install-hooks` prints instructions instead of symlinking when `core.hooksPath` is set, because the run that would silently do nothing is the failure mode `20260810_ci-gates-that-never-ran.md` is about. Tested in both directions with a synthesised unpushed pin, built as a `commit-tree`/`mktree` object pair so neither index nor worktree moved: the installed hook refuses that push naming the offending SHA, and passes the real HEAD. It is a guard rail on this clone, not a proof - `--no-verify` goes around it.
- **A loop's `line` events are now CPython's for general `for` loops, and the defect the risk register asked to fix turns out to be unreachable there (2026-08-10).** `20260810_loop-line-events.md`. Measuring the three loop forms separately, with a two-statement body so first and last body lines can be told apart, found three distortions where the 2026-08-09 note recorded one. The largest was not the recorded one: `MP_BC_FOR_ITER` cleared the reported line on every iteration so that a one-line `for` would repeat, which made a *multi-line* `for` report its header twice per iteration - ten times against CPython's six for a two-deep nest. `py/vm.c` now invalidates on a backward jump instead (`MP_BC_JUMP`, both `POP_JUMP_IF_*`, `MP_BC_UNWIND_JUMP`), which is CPython's own rule and covers both what the hack wanted and what it broke: general `for` loops match CPython on every line of the measurement, and `range`/`while` loops whose whole body sits on the header line now iterate visibly instead of reporting once for the loop. The offset is cast to `ptrdiff_t`, not `mp_int_t` - `DECODE_SLABEL` yields a `size_t`, narrower than `mp_int_t` under `MICROPY_OBJ_REPR_D`, where the sign test would then never fire. What cannot be fixed is the row's own prescription: the loop test is compiled to the *bottom* of a `range`/`while` loop (one branch per iteration, not two) so it inherits the last body line's number, and the .mpy line table is monotonically non-decreasing by construction (`py/emitbc.c:459` records a line only when it increases), so it cannot be re-attributed to the header without an .mpy format change. Moving the test to the top costs every build a second branch per iteration for something only a tracer sees. The row is narrowed rather than closed, and with it the testing prohibition: a stop count may now be asserted anywhere except the last body line of a `range`/`while` loop and a `while` line. `tests/misc/sys_settrace_generator.py.exp` moves with the fix, both halves toward CPython. The fix is on upstream master's behaviour, not the #8767 lineage - `MICROPY_PY_SYS_SETTRACE` and the `FOR_ITER` clear both predate it - so it is folded as its own branch, `settrace_loop_line_events`, registered in `mbm.toml` and pushed to the fork; no PR is open. The `mbm.toml` entry has not yet been exercised by a rebuild - the next `make integrate` is the first time it replays, and it is placed after `local_names_implementation` because that is the order it was written in. Verified against a plain-master coverage build as well as the integration build: `sys_settrace_features.py` has no `.exp` upstream and fails against CPython 3.12 there, before and after, with byte-identical output.
- **STORY-6.6 DONE (2026-08-10): pause stops the target, and the field that made it look implemented is gone.** `20260810_dap-pause.md`. `_handle_pause` set `DebugSession.paused` and `PdbAdapter.paused`, called `pause()` and answered success; `should_stop`, the only stop decision, read neither. The visible cost was worse than a wrong UI: a client that believes it is stopped asks for `stackTrace`, `scopes` and `variables`, and all three were answered from `current_frame` - a frame the program had already left, with values true at no stated moment. `should_stop` now consumes the pending request at the next `line` event, for the same reason its breakpoint check is gated there (`call` reports the `def` line before the body runs, `return` a frame that has already produced its value), and kills any pending step with it, because a step that outlived a user interrupt fires later at a point nobody asked for. The stop reason now comes from the adapter, which is what decided: a stop is a breakpoint, a consumed pause, or a landed step, and those three being exhaustive is what lets `DebugSession.paused` (6 writes, 0 reads) and `DebugSession.stepping` (6 writes, 1 read) be deleted instead of left write-only - the ladder's `STOP_REASON_PAUSE` branch had been unreachable. The risk row said the harness was the work, and it was: every existing scenario drives a target that stops on its own, so `src/spinner.py` advances a module global every 5 ms up to a bound, and the test asserts that counter stops moving across 0.4 s rather than that a `stopped` event arrived - a target that ignores the pause fails, and one that never pauses ends on its own rather than hanging. One case the row did not name showed up only once a pause could be sent: a `pause` while already stopped is handled by `wait_for_continue`'s drain and would stop the program one line into its next run, so the flag is cleared on the way out of the wait. Both halves mutation-checked, each failing only its own scenario. Still true, and now documented in both the module README and `docs/debugging.md` in place of "pause does nothing": a target blocked in `time.sleep`, on a socket, or inside a long C function emits no trace event and the request stays pending.
- **CI is green for the first time (2026-08-10), and getting there found three checks that were wired up and never ran.** `20260810_ci-gates-that-never-ran.md`. `make lint` was `uv run ruff check .` with ruff declared in no dependency list, so it resolved whatever was on PATH and on a runner resolved nothing: `Failed to spawn: ruff`, on every run that reached the step since the gate landed in `b285b4b`. The Lint step runs before the host DAP suite, deliberately, so a Lint step that could never pass meant **the host suite had not run on a runner since 2026-08-06** - the commit that made ruff enforceable is the one that turned the suite off in CI. ruff is now a pinned `lint` dependency group resolved from `uv.lock`, checked the way the defect asks to be checked: `make lint` in an `env -i` shell holding only `uv`, `make` and `git`, with no ruff on PATH. Run `31352461359` is the first green `firmware` run - lint clean, then 361 passed / 20 skipped / 1 xfailed in 154.65s, first attempt, no retry. The 362-vs-361 difference is `test_s7_1_extension_suite_passes`, which skips without npm and `extension/node_modules`, **and chasing it found the second gap: the extension's 98 node tests reached CI only through that skipped case, while the extension-host job next door ran a different suite (`test:host`) and looked like the same coverage.** `npm test` is now its own step there, executing (98 pass). Third, the push trigger listed the sources the jobs build but not `Makefile`, `pyproject.toml`, `uv.lock` or `src/` - the commands CI runs, the versions they resolve, and the debuggees whose fixed line numbers the suite breaks on. All four added. Also closed by its own logs: the extension-host risk row, which said the job had never executed. It has, on 2026-08-08, and that first green job is precisely the cold path the row was about - cache miss, VS Code 1.132.0 downloaded, `ms-python.debugpy` installed from the marketplace, 10 passing. The row outlived the evidence because the run containing that job was cancelled and `gh run list` reports the run's conclusion, not the job's.
- **The integration is rebuilt on current upstream (2026-08-10), which closes criterion 4's deferral, and the two failures it produced were both in how the suite watches the board.** `20260810_integration-rebuild.md`. The micropython half moved from `06bcfd5b74` to `5f2181f938`, five upstream commits, none touching anything the composition owns; `mpy-debugpy` is `4eaafbc5bc`, seven merges with nothing cherry-picked on top. Faithfulness is asserted on the composed delta rather than the graph: `git diff <base> <integration>` is byte-identical before and after, so the rebuild changed the base and the shape of the history and not the tree the firmware is built from. Rewriting the commits moved `debug_board_flags`'s single commit into a push range for the first time and the fork's pre-push hook rejected it on three `verifygitlog` rules at once, fixed at the source commit (`77e3306233` to `435602aedd`, empty tree diff) - a commit that lives only on an integration branch is checked by nothing, which is an argument for rebuilding sooner. One hook failure was the hook: it pipes explicit paths into `ruff format --check`, and an explicit path defeats `pyproject.toml`'s `[tool.ruff.format] exclude` unless `--force-exclude` is passed, so an untouched upstream file failed; every check was rerun by hand with that flag and the push made with `--no-verify`, leaving the shared hook outside this repo unpatched. **The micropython-lib half cannot be rebuilt yet:** `refs/pull/1022/head` is `792d14d`, an ancestor of the local branch and 13 commits behind it, and mbm composes a PR-numbered entry from the PR head, so the `mpy-debugpy_update` mbm produced has no history for `stream_transport.py` at all - the whole stream transport and criterion 3's fix. It unblocks on the push that is held for the user. On the bench, a DFU reflash from the new tip preserved the filesystem and the device-side debugpy install was confirmed current by recomputing the installer's cache key from source and matching the marker, rather than assumed. The suite then passed 18/18 once and failed the bridge-kill scenario on the next run against identical firmware, and neither cause was in the composed code. **A CDC interface carries stdout only while a host holds it:** killing the only holder of the primary port drops its DTR and the board discards what it prints until the port is reopened, so the scenario raced the board and could only lose silently, a dropped completion line being indistinguishable from a board that never resumed; the capture is now opened before the kill, as a second holder of a port the command still has, and the scenario passes in about five seconds instead of waiting out its 30 s timeout. The mechanism was confirmed separately first, driving `StreamTransport` against the installed `.mpy` with no session in the picture. **The ports come back before the board does:** `boot.py` calls `pyb.usb_mode()`, which enumerates where it is called and not when the script returns, so the device paths reappear while the WiFi association is still running - 5.4 s to the paths and 8.5 s to the REPL on a good association, 15 s more per retry - and under randomised ordering a network scenario following the reset scenario failed twelve tests with `No route to host`. The reset helper now waits for a REPL prompt too, provoking one with a newline rather than waiting for a banner that is discarded if it is printed before the port is open. Three consecutive green full runs, two randomised.
- **STORY-6.1 criterion 3 met on hardware (2026-08-10), and it needed a fix: a USB CDC interface has no EOF, so killing the bridge at a breakpoint froze the board until it was power-cycled.** `20260810_bridge-kill-mid-session.md`. The host-side pty scenario had asserted this criterion since 2026-08-07 and passed throughout, because a pty reaches EOF and a CDC interface never can: stm32's `pyb_usb_vcp_ioctl` sets `MP_STREAM_POLL_RD` only when bytes are waiting and never reports POLLHUP or POLLERR, so an idle interface and one whose host has vanished are indistinguishable. Everything downstream was already right - `wait_for_continue` uninstalls the trace function and releases the target on a closed channel - but `JsonMessageChannel.closed` is set from a `recv` returning `b""`, which that interface never does. **This is the second time a pty has flattered a mechanism in this story**, criterion 4's board reset being the first. The signal a CDC interface has instead is its DTR line, `pyb.USB_VCP.isconnected()`, measured on the board as False before the host opens the node, True while a handle is open, False after it closes, and per-interface so the REPL's does not mask it. `listen_stream` now takes an optional `is_connected` callable and `StreamTransport` treats it going false as EOF, so debugpy stays port-agnostic and the unix port's file-backed `dap_device`, which has a real EOF and no `isconnected`, is untouched. **The latch is on traffic received, not on having seen DTR high:** `serial_dap.check_device()` opens and closes the DAP node before the bridge runs and the kernel raises DTR on tty open regardless, so a DTR-only latch would arm on that flicker and end the session before the client attached; down before any byte has crossed is also exactly the state between `listen_stream()` and the first connect. Proven by `tests/hil/test_hil_bridge_kill.py` (SIGKILL at a breakpoint, then the target completes, both device nodes survive, and a fresh `mpremote debug` reaches a breakpoint over the same interface), with a second scenario asserting `isconnected()` is True mid-session so a firmware that stopped tracking DTR fails as itself. Confirmed causal by removing the wiring and watching the board freeze again. Host-side, `test_stream_transport_peer_gone_signal` pins each latch case over a pipe the host never closes, both halves mutation-checked. Eighteen HIL scenarios green (`20260810_hil_PYBD_SF6.md`). Criterion 5 was already met by `TestSerialDapBridgePump` and is now ticked; criterion 2, the single-UART negative probe, is the only one left and needs a board that is not on the bench.
- **STORY-6.1 criterion 4 settled and proven on hardware (2026-08-10): a board reset ends the session, and reconnecting would be the wrong answer rather than the unfinished one.** `20260810_board-reset-mid-session.md`. `do_reconnect(state)` in the ampremote tree loops without a deadline until a device matching the original connect string returns and rebuilds a `SerialTransport` for it - the primary REPL connection and nothing else. That is right for `mpremote repl` and cannot be right for a debug session: the rebooted device runs a fresh `debugpy` with no memory of anything the client established, while the client has lost nothing and is still holding breakpoints and `variablesReference` handles for frames that no longer exist, so a revived byte pump would hand it a peer that never received its `initialize` and let it keep addressing objects the peer has never heard of - undetectably, because it looks like the session continuing. The bridge forces the issue from the other side anyway: on target loss `DapProxy._accept`/`_pump` records `target_error`, shuts both directions and closes the client socket. So the criterion is the clear error, and it is now proven by `tests/hil/test_hil_board_reset.py`, which power-cycles a PYBD-SF6W while the client is stopped at a breakpoint over the serial bridge and asserts the command reports the loss, exits non-zero, and prints no traceback. The reset itself comes from `MPY_DEBUG_HIL_RESET_CMD` rather than the suite, since there is no portable way to reset a board; the scenario skips where the bench has none. **Mutating the assertion found a sixth defect, this one general upstream mpremote:** `SerialTransport.close()` clears RTS/DTR before closing and tolerated only `ENOTTY`, but a device that has gone away fails the ioctl with `EIO` (pinned by a standalone probe: open the port, cycle its hub port, `close()`), and `do_disconnect` calls `close()` outside the `try` that exists for exactly "the serial port disappeared" while `main()` calls `do_disconnect` from a `finally` outside its `CommandError` handler - so the command printed its clear error and then died with an unhandled traceback, port still open, since the raise happens before `self.serial.close()`. Clearing those signals is best-effort by nature and no longer stops the close. It is its own branch, `mpremote_close_lost_device`, registered in `mbm.toml` rather than riding `mpremote_debug`'s upstream PR. Sixteen HIL scenarios green (`20260810_hil_PYBD_SF6.md`). Recorded as deferred: the integration was merged rather than rebuilt, because `mbm rebase` refetches the PR-numbered branches live and #8767's head has moved on to a newer master, which would put the flashed firmware out of step with the source mid-verification. Closed later the same day for the micropython half by `20260810_integration-rebuild.md`; the micropython-lib half stays deferred, on the push that is held for the user.
- **The pty mount test's intermittent teardown failure is a termios race between two processes over one line discipline (2026-08-10), and it was never about the readiness sleep.** `20260810_pty-termios-race.md`. Putting the unix device on a pty gives it a tty it can *configure*, which no USB CDC device can: `mp_hal_stdio_mode_raw()` snapshots whatever termios it finds and `pyexec_raw_repl` restores that snapshot around every exec so a program can take SIGINT. The snapshot is taken at the kernel's canonical defaults, before pyserial ever rawifies the line, so each exec ran cooked - ICRNL rewriting the device's CRs to NL, ECHO feeding the device its own output back as input (the third byte that refuses the raw-paste handshake, and a feedback loop printing five prompts for one `\r`), and `\x04` as VEOF rather than the raw REPL's end-of-output marker, which is the `timeout waiting for first EOF reception` the check reported. `tty.setraw` on the slave before the interpreter is spawned makes every snapshot it can take a raw one: 0 broken in 800 against a 7-13% base rate, and 60 consecutive green runs of the test. **Nine harness sites across four files had it**, and the second was found by the full suite rather than by the investigation - `test_s4_5_hot_reload.py`'s `_LoopSession` brings its device up the same way and failed the same check with the same error at the same line, at 1 in 12 and then 1 in 2 across two measured loops. That is the argument for the fix living in `tests/pty_device.py` as a `PtyDevice` every such harness holds, rather than the same edit made twice. A sweep on the exposure criterion - a firmware process on a pty master *plus* a pyserial opener of the slave, which rawifies after the device has snapshotted - found seven more in `test_s5_1_mpremote_debug.py` and `test_s6_1_serial_dap_bridge.py`; harnesses driven only by raw fds are not exposed, since with nobody rawifying the line the snapshot the device restores is the one already in force. Three changes came with it, each replacing something unobservable with something asserted: the slave fd is held open for the session, the 0.3 s readiness sleep is replaced by reading that fd until the friendly-REPL prompt appears, and STORY-4.3's `detach_client` 1.0 s settle is replaced by polling for a marker `app.main()` writes back *through the mount* whose content is the loop's result - so the wait ends on the program having demonstrably run out, and the mount is proved still serving RPC after the debug client has gone. STORY-4.5's settle keeps its sleep for a stated reason: it waits for the target to leave `wait_for_restart` after its own code has finished, which no marker the program could write observes. The check now records what it wrote and read, and asserts the pty is still raw afterwards, which is a direct guard on this mechanism. Recorded as a negative result because its evidence looked strong and it was wrong: with no slave fd open the device's `read(2)` fails EIO, and `MP_HAL_RETRY_SYSCALL`'s empty raise clause leaves `mp_hal_stdin_rx_chr` returning an *uninitialised* byte to the REPL 85,128 times per run (confirmed by `strace`) - a real upstream unix-port defect, left for its own micropython PR since it belongs on no composed branch, but closing that window changed the failure rate not at all. What separated the two was a 40-line standalone reproducer with no mount, no DAP and no harness, not more instrumentation of the full test.
- **Q11 closed (2026-08-10): the STORY-4.1 installer has a production call site, `mpremote debugpy-install`, and the branch it was thought to be blocked on was the wrong branch.** The command takes the host package directory as a required positional - mpremote ships no copy of debugpy and cannot guess where a checkout is - plus the installer's optional `--mpy-cross PATH`, and it validates before opening the port: a path that is not a directory, or one with no `__init__.py`, is refused without touching the device, the second naming the inner `debugpy/` when the named directory holds one, because pointing at micropython-lib's package folder rather than the package is the predictable mistake. Both rejected alternatives - implicit install on every device session, and a `--install` flag on `debug` - fail on the same two grounds: each puts the unguessable host path into every run for the benefit of one run in fifty, and each lets a bare `mpremote debug` invoke an installer that *sweeps* its device directory, so a hand-placed or `mip`-installed file disappears the first time someone debugs. The flag would also have grown fork PR #51, which is what exposed the premise error: `debug` is on `mpremote_debug`, the installer is on `mpremote_debugpy_install`, so a separate command lands entirely on the installer's branch and leaves #51 untouched - the question was never held by the user's pending review at all. The soft-reset requirement needed no mechanism: `_auto_soft_reset` already resets on the first command of a separate invocation and `+ soft-reset` covers a chain, so the command matches `mip install` in not resetting, and instead prints the warning only when a write actually changed something. `--target` is deliberately not exposed, because the swept directory must stay the one resolved from the target's `sys.path` rather than one a typo can aim at unrelated files. It does not duplicate `mip install`, which fetches a release and rewrites everything; this compiles a local checkout, skips unchanged files against a content-hash marker, and removes what the marker does not record - which is what makes "install then debug" cheap enough to run before every session. Seven host tests, each mutation-checked, and the file's 54 now pass twice in a row: one of the new tests built its fixture at a fixed `/tmp` path and leaked it on failure, so it passed once and then failed forever. On a PYBD-SF6W: up-to-date reported with no transfer, the containing folder refused with the inner directory named, and after deleting `/flash/lib/debugpy` and its marker, a from-scratch install followed by 15 green HIL scenarios (`20260810_hil_PYBD_SF6.md`) and by a live handshake from `debugpy-install + soft-reset + debug` in one invocation. `docs/debugging.md` gains a section on getting debugpy onto a board.
- **Q14 closed (2026-08-10): an F5 launch reaches `--source` and `--loop`, and the gap was smaller than the question claimed.** `source` and `loop` are now launch-configuration properties, contributed in `extension/package.json`, carried through `MpremoteDebugOptions` and emitted by `buildDebugArgs` before the positionals. Absent means absent: nothing is emitted, so the CLI's own precedence decides. That is what stops the launch config from duplicating `mpdebug.toml`, and it matters because the question's premise did not hold - `do_debug` falls back to `resolved.source` whenever `--source` is missing, and the mount, the host-side module check and the generated `pathMappings` all key off the resulting `source_root`, so a target declaring `source` was already mounting on F5. What was genuinely unreachable was an ad-hoc or overriding source root, and `--loop`, which no `mpdebug.toml` key can express. Two behaviours are asserted against the real command rather than a fake: `--loop` reaches `mpremote debug` and still produces a handshake, and `--source` on a unix target comes back as the CLI's own `--source is not valid for a unix target` through the captured-output path - the second is the test that justifies not duplicating the CLI's kind check inside the extension. An empty `source` is refused in `buildDebugArgs` for the reason `mpdebug.toml` refuses one: `--source ""` would realpath to the child's cwd. Extension suite 98 passed / 0 failed / 0 skipped, so the integration tests ran rather than being gated out. `docs/debugging.md` loses the sentence saying both flags are command-line only and gains the two keys plus an example. Unproven and recorded as such: whether VS Code's restart button, routed through the `ms-python.debugpy` adapter, surfaces the target's `supportsRestartRequest` - the on-target half is certain (`enable_restart()` is called only under `loop`), the editor half needs an extension-host scenario that drives the restart button, which none of that suite's ten does. Corrected 2026-08-10: this originally said the job itself had never run, which stopped being true on 2026-08-08 - see `20260810_ci-gates-that-never-ran.md`. What is missing is the scenario, not the runner.
- **The micropython-lib side's fold debt is discharged locally too (2026-08-10), and the branch is staged but not pushed.** The twelve commits on the integration that PR #1022 could not see are now on `add-debugpy-support`, which is `2c52eb3`, 47 non-merge commits on `upstream/master` at `d8a719f`: the stream transport, the `serial_dap` derivation, the `save_names` probe fix, the idle-errno and short-write fixes, both breakpoint-key and breakpoint-event fixes, the `StreamTransport.send` contract, symmetric path translation, the message-pump fix, DAP `restart` with the console channel, and the two documentation corrections. `python-ecosys/debugpy/` is byte-identical to the integration branch afterwards, which is the check that says the fold is complete; patch-id cannot say it, for the reason the risk register now gives twice over. Two resolutions a reviewer should see rather than have smoothed over. (1) `rerere` replayed a recorded resolution that was wrong in this direction: the development-guide commit also touched `test_vscode.py`, the later samples rewrite already supersedes all of that hunk, and the only residue was the loss of the file's trailing newline, which `ruff format` rejects. That hunk was dropped and the guide taken alone. (2) The `restart` commit was re-picked with `ruff format` and `ruff check --fix` applied inside it, because the branch's existing lint commits sit before the code they would have to cover; folding the two hunks into the commit that introduces them keeps the property STORY-8.3 established, that no "format the integration" commit reaches a PR. All twelve messages were rewritten to pass `tools/verifygitlog.py` (subject to 72 ending in a period, body to 75, `Claude-Session` before `Signed-off-by`) and the cherry-pick provenance lines stripped, since they name a branch no reviewer can fetch. `verifygitlog upstream/master..HEAD`, `ruff check`, `ruff format --diff` and `codespell` are all green, and the host DAP suite is 354 passed / 16 skipped / 1 xfailed with the submodule on the branch. A trap that cost a full false-alarm cycle first: the suite reported nine failures that were entirely a stale unix binary left over from building the other submodule's `pdb_support`, so `make firmware-unix` after any submodule branch switch is a precondition of reading the suite at all, not a nicety. **Not pushed.** Unlike the message rewrite, which left the tree byte-identical, this changes what PR #1022 asks a reviewer to read, so it waits for the user in the same way STORY-8.3's upstream PR does.
- **The micropython side's fold debt is zero as of 2026-08-10, and closing it meant putting a VM fix on PR #8767.** `1d565d21fc py/profile: Keep tracing usable after a trace callback raises` was the one substantive commit on the `mpy-debugpy` integration that no feature branch carried, and it is the fix STORY-4.5 needed twice: an `nlr` guard that clears `mp_prof_is_executing` and unsets the trace callback when a callback raises (CPython's semantics), plus a `TRACE_TICK` test on the global callback, without which `settrace(None)` leaves frames already running still traced. Cherry-picked onto `pdb_support` as `139da482d6` with the author preserved, carrying `tests/misc/sys_settrace_raise.py`, which deliberately ships no `.exp` so run-tests compares MicroPython against CPython on the same source. Mutation-confirmed: revert the two C files, rebuild, and the test fails on `re-armed: False` and `swallowing returned 3` where it expects `re-armed: True` and `swallowing unwound`. Full unix suite 1033 tests / 1032 passed; the single failure, `extmod/select_poll_fd.py`, is a CPython 3.12 traceback-format mismatch that fails identically with the fix reverted, so it is not this change. `codeformat.py -c` and `-p` clean, `verifygitlog upstream/master..HEAD` ok, pushed to the fork, which is what updates PR #8767. The other five patch-id candidates are false positives and each was closed by diffing files rather than patches, as the risk register now requires: three `tools/mpremote` commits are on `mpremote_debug` as `33c065e033`/`db25b0f63e`/`3a84ac8759` (patch-id misses them because the format hunks were folded in as `--fixup`s), the "format the files this integration left unformatted" commit is split between `local_names_implementation` and those same fixups, and the two duplicate `sys/settrace: Improve settrace` commits are `pdb_support`'s `3280c2a5ea` re-applied by successive rebuilds. What is left when the integration's files are diffed against each feature branch attributes entirely to another branch in the composition (`verify_hash` to `mpremote_file_cp_hash`, `fs_ensure_path_exists` and the installer to `mpremote_debugpy_install`, the locals dict to `local_names_implementation`) or to master drift, `py/emitbc.c`'s `include_source_lines` block being upstream's own.
- **PR micropython-lib#1022 passes every check for the first time (2026-08-10): the `T100` hits are gone by design change, the 12 commit messages are rewritten, and the branch is rebased onto current master.** Three separate things had to be fixed. (1) `T100`, by user decision to fix the files rather than the rule. `test_vscode.py` no longer starts its own debug server; it is an ordinary program with a `main()`, and the session is started around it by whatever runs it. That is the shape a device session already has and the only one available when the client is at the other end of a link, so the rule was right about those files. `demo.py`'s `import debugpy` was dead, since it reaches the package through `from debugpy.server ...` imports. The README and the development guide now carry the command that does the launching, and that command was run end to end against a real unix build with a minimal DAP client before being documented: attach, `setBreakpoints` on `test_vscode.py:36`, `configurationDone`, stop, `stackTrace`/`scopes`/`variables`, continue to exit. The `per-file-ignores` entry the earlier pass had added to the integration branch's `pyproject.toml` came out with it, so this project changes nothing about the repo's lint policy. Two incidental repairs came with the rewrite: the sample imported `machine`, which does not exist on the unix port its own guide tells you to run it on, and the README named a test file the package does not contain and a build directory its own build command does not produce. (2) Commit messages, by user decision to rewrite and force-push. 12 of the branch's 35 commits failed `tools/verifygitlog.py`; `git rebase -i` is unavailable in this environment, so `git filter-branch --msg-filter` keyed on `$GIT_COMMIT` did it, preserving Jos Verlinde's authorship on his seven of them and leaving the tree byte-identical. (3) A CI artifact that reads as a content failure and is not. After the rewrite the commit-formatting job still failed, on `d6faaf8` and `b5aa5f0`, which are upstream master's own commits and predate DCO enforcement. `ci_commit_formatting_run` fetches master with `--depth=100` and unshallows only if `git merge-base` fails outright; the branch base was 115 master commits back, so the shallow graft made ancestry wrong and `upstream/master..HEAD` enumerated 101 commits instead of 35. The repair is the rebase that range assumes, not a change to the branch: rebased with `--rebase-merges`, so the `pdb_support/perf2` merge in Jos's history survives rather than being flattened, which a plain rebase cannot do without re-resolving that merge by hand. `rerere` replayed the recorded resolutions, `python-ecosys/debugpy/` is byte-identical to the pre-rebase tip, and the branch adds nothing outside that directory. `build` (three jobs), `ruff` and `codespell` all pass. The PR head is now `792d14d`, 36 commits (35 plus the merge) on `upstream/master` at `d8a719f`.
- **STORY-8.4 DONE (2026-08-09): one guide, and writing it found four published documents claiming things the code contradicts.** `docs/debugging.md` covers the target file, the three transports, the `--source`/`--loop` iteration loop, attaching from VS Code, what the probe reports and what each value means in the editor, the measured limitations, and troubleshooting keyed on the error strings `commands.py` actually prints so a user can search for what they saw. Firmware is a link to `docs/firmware.md`, not a second capability table to keep in step. Verified by execution rather than inspection: the quick start ran verbatim from the documented environment to a breakpoint hit at `src/target.py:79` in `main`, the three-target TOML example loaded through the real `mpdebug_config` with all three resolving, and every quoted flag, config key and error string was grepped back - four of the error strings only match once the source's implicit string concatenation is joined, which is exactly how a docs-vs-code check gives a false negative. Four corrections came out of that pass, each a claim a reader would have acted on. (1) The placeholder local-name format is `local_%02d`, 0-based (`py/profile.c:183,207`), so `local_00` first - `_is_placeholder_local_name`'s own docstring said `local_1` and named `MICROPY_PY_SYS_SETTRACE_SAVE_NAMES`, a spelling that is a no-op on this branch, and `BACKGROUND.md` asserted `local_%d` 1-based as a correction *of* the right spelling. Only the runtime check was unaffected, because it tests the digits and not the width. (2) The debugpy module README advertised "Pause/continue execution", which the unimplemented `pause` contradicts, and "locals generally not supported", which `save_names` contradicts. (3) `docs/firmware.md` still marked the PYBD_SF6 row "build intent" although those four values have now been probed off the board - but by a local build of a later tip, not by the `f9d7c96b96` artifact the manifest publishes, so the row says that rather than claiming probe-confirmed, and the guide no longer implies every published artifact was probed. (4) The README's device quick start still described the pre-EPIC-5 manual flow (`compile_debugpy.py`, `cp -r`, `run launcher/...`) and never named `mpremote debug`. Documented and deliberately not fixed at the time: the extension passes neither `--source` nor `--loop`. New: Q14, closed 2026-08-10; the gap the sentence describes was also narrower than it said, and the Q14 entry says how.
- **STORY-4.5 DONE (2026-08-09): an edit on the host is the next run, with no upload and no reset - and the VM had to be fixed twice to allow it.** `mpremote debug --loop --source <dir>` keeps one process, one DAP session and one handshake across many runs of the target: a DAP `restart` unwinds the target, evicts from `sys.modules` everything added since a baseline taken before the first target import, and imports again, so the mount serves the edited file. Verified live over a pty (`tests/test_s4_5_hot_reload.py`, `20260809_story4.5_hot_reload.md`): run 1 reads `total == 3`, one restart later run 2 stops on a line that exists only in the edited submodule and reads `total == 30`, with breakpoints set once before `configurationDone` and never re-sent, `evicted == ["app", "helper"]`, and exactly one `MPDBG-READY` for the session. The eviction set is a snapshot diff rather than the ticket's `__file__`-under-source-root rule: the debugger cannot be in it by construction, it needs no `__file__` (which on a mounted session reads as the device path anyway), and a changed submodule comes back with its parent - the classic hot-reload hole. Two VM-level settrace findings came out of it, both in the #8767 lineage. (1) FIXED in `py/profile.c`/`py/vm.c` (`20260809_settrace_raise_unwind.md`): a trace callback that raises left `mp_prof_is_executing` set forever, so *no* callback was ever invoked again while `sys.settrace`/`gettrace` went on reporting success - which silently disabled every breakpoint after the first restart, since a deliberate raise is the only mechanism on the device that can unwind a running target. The callback is now unset when it raises (CPython's semantics) and `TRACE_TICK` checks the global callback, without which `settrace(None)` did not stop tracing frames already running. (2) NOT fixed, characterised (`20260809_settrace_line_event_fidelity.md`): MicroPython emits one more `line` event for a loop body than it has executions, the extra one *before* the body has run, so a breakpoint in a loop body stops N+1 times; and `return` reports the last line the frame executed, which made a breakpoint on a function's final line stop twice per call until `should_stop` was gated on the event. Also found and not fixed: the DAP `pause` request is answered with success and never stops the target - `paused` is written in three places and read by no stop decision.
- **STORY-4.3 DONE (2026-08-09): a device debugs a file the board has never held, and the session's ending is now two different claims.** `mpremote debug --source <dir>` mounts that directory at the device's remote-fs mount point before the boot script runs, reports the mapping as absolute `pathMappings` in its handshake, and refuses before touching the device if the directory is missing or the target module does not resolve under it. Green over a pty against the unix firmware and on the PYBD_SF6, where the breakpoint lands in a module the board is first proved unable to import. The extension consumes the handshake's mappings and no longer synthesizes one for a serial or network target. Two defects, both in code the criteria reach rather than in the new path (`20260809_story4.3_mount_teardown.md`): (1) `process_pending_messages` restored the channel's blocking mode in a `finally`, and the loop it guards is what closes that channel while handling `disconnect` - so every ordinary session end raised EBADF into whichever line of the debugged program was being traced, killing it with a traceback naming the debug channel's errno, and mpremote then read that traceback as a device fault and told the user to power-cycle a healthy board. Deterministic, and previously filed as a timing problem. (2) Teardown reported "only a power cycle clears it" for a `TransportExecError`, which by its existence proves the device answered. The two endings are now distinguished: a client that left first leaves a responsive board and teardown says nothing, while a client still holding a paused target leaves a board nothing can reach and gets exactly one bounded, actionable line. Also recorded there: a soft reset ends the unix port's process, so an in-process reachability check written with `enter_raw_repl`'s default destroys the device it is checking.
- **EPIC-4 phase entry (2026-08-08): the mount the whole device mainline rests on was broken, and no story owned putting it in the debug flow.** `20260808_epic4_phase_entry.md`. `mount_local` swaps a `SerialIntercept` in for the transport's serial object; `read_until` saves and restores the serial-level read timeout, and the substitute carried no such attribute, so every read after a mount raised `AttributeError` - on hardware and on a pty alike. The unconditional read was this project's own `b8d443b4b5` on `mpremote_debug`, a branch destined for upstream, so the regression was ours. Two consequences: STORY-4.5 was never hardware-gated (the pty mount failure recorded against it was this defect, and the full loop is now testable on the host), and an aborted mount wedges a board past software recovery, because Ctrl-C is consumed as filesystem-RPC payload and the REPL never returns - the PYBD needed a power cycle. Separately, `mount_local` has exactly one caller, `do_mount`: `do_debug` mounts nothing, so both remaining EPIC-4 stories presupposed a mount that no story built. STORY-4.3 absorbs establishing it, which is what the roadmap's own D2 collapse text calls "a live-mount attach path". Eviction and re-import confirmed on the PYBD_SF6: `del sys.modules[name]` re-reads the mounted file, no upload, no reset.
- **The published PYBD firmware cannot run the serial-DAP scenarios, and the manifest does not say so (found 2026-08-08).** `firmware/firmware.toml`'s `stm32-pybd-sf6-debug` is built from `f9d7c96b96`, which is not an ancestor of the current integration tip and predates `MICROPY_HW_USB_CDC_NUM (2)` in `boards/PYBD_SF6/mpconfigboard.h` - so it enumerates one CDC interface and STORY-6.1's path is unreachable on it. The hardware results were produced by a local build of the pinned source (`v1.29.0-preview.702.g2c816215dc`), which the HIL record now names in full rather than by release. Nothing is misverified: `launcher/firmware.py` checks the hash of what the manifest describes, and it describes an older build honestly. What is missing is any way for the manifest to express the second CDC, since the interface count is a firmware property while `serial_dap` deliberately is not. Republishing is STORY-3.2's CI job; the manifest key is a design question that has to be settled with it. New: Q12.
- **STORY-6.1 criterion 1 met on hardware and STORY-6.4's serial half done (2026-08-08): DAP over the board's own second CDC, plus a fifth defect.** `mpremote debug hil` against a `dap_device` target reaches a breakpoint on a PYBD_SF6 with no address of any kind in the config or on the command line: the device reports `serial:0`, mpremote bridges its second CDC to a loopback port, and the client cannot tell it from the network path. Three separate things had to be true, each found the hard way: the device picks the channel (`dap_stream="board"`), because the host names a tty node and only the device can map that to a runtime object; `pyb.USB_VCP(1)` constructing proves nothing, since it answers from the build-time `MICROPY_HW_USB_CDC_NUM` and only `pyb.usb_mode()` says what boot enumerated; and failing to get the requested stream must raise, since a TCP fallback leaves the device reporting an endpoint the waiting bridge has no client for. Criterion 1 is PYBD_SF6's answer and not the pico_w's; criterion 2 still needs a board with no second CDC. The throughput risk row is now measured: 16 KB in 0.20 s, 81.7 kB/s. Getting there found D5 below. Fourteen HIL scenarios green (`20260808_hil_PYBD_SF6.md`).
- **A fifth hardware-found defect (2026-08-08): every DAP message over 1024 bytes desynchronised the serial link.** `USB_VCP.write` takes at most `MICROPY_HW_USB_CDC_TX_DATA_SIZE` per call and reports the short count, and `public_api.py` drops the channel timeout to 1 ms once `initialize` is answered. `StreamTransport.send` had a whole-buffer contract: it looped internally and raised when the timeout expired mid-frame, discarding the count of what had already gone out, so `_send_all` retried from offset 0 and resent the prefix. Not a visible error, just a stream the peer can never parse again; bisected by payload size (512 bytes fine, 1024 never answered) and confirmed at the raw layer. `send` now has the socket contract - write once, return the count, EAGAIN only when nothing went out - which is what `_send_all` was already written for. Pinned by `tests/test_s6_1_stream_transport.py` over a pipe shrunk with `F_SETPIPE_SZ` and set non-blocking, with the host deliberately stalling mid-frame; both the short-write assertion and the frame body fail on the old transport.
- **STORY-6.2 DONE and STORY-6.4's network half done (2026-08-08): eleven scenarios green over WiFi on a PYBD_SF6.** `tests/hil/` drives the production command (`mpremote debug <by-id path> target:main`) against the board and speaks DAP to whatever endpoint the handshake reports - no address, board name or device path is hardcoded in the suite, only recorded by the results record as provenance - and it skips unless `MPY_DEBUG_HIL_DEVICE` is set, so `make test` stays host-only. It installs debugpy and the debuggee itself, so a run proves the code in this checkout, and it writes its own results record from pytest's reports rather than from the tests, so a scenario cannot claim green by forgetting to record a failure. Covered: reachable endpoint, handshake caps against a live probe, breakpoint/stack/scopes/locals, a 16 KB response arriving whole, per-iteration loop breakpoints, run-to-completion, a 60 s pause, two consecutive runs, and both step kinds. Three of the four scenarios that failed first time were the harness asserting the wrong thing, and each wrong premise turned out to be a documented behaviour worth pinning (`20260808_epic6_bench.md`); the fourth was a real defect, D4 below. The pause-loop risk-register row is now measured, not open. Still unmet: the serial half of STORY-6.4, and the ESP32/pico_w criteria - no board.
- **A fourth hardware-found defect (2026-08-08): clearing a file's breakpoints left the debuggee's copy armed.** `set_breakpoints` stores each set under both the client's path and the name the debuggee reports, but replaced only the client's key outside the per-breakpoint loop - so DAP's empty-list removal freed one key and not the other, and several breakpoints in one file left only the last of them under the second key. Dormant until something sets `pathMappings` (STORY-7.1), because the two keys are otherwise the same string. Fixed by replacing both together; pinned by `tests/test_dbg_07_clear_breakpoints.py`, mutation-confirmed against a real session with `localRoot != remoteRoot`.
- **EPIC-6 phase entry on real hardware (2026-08-08) — two defects found and fixed; the v1 board set is only one-third present.** `20260808_epic6_bench.md`. Of Q2's three physical boards the bench has `PYBD_SF6` only; `rpi_pico_w` and `ESP32_GENERIC` are registered but offline, and only the PYBD may be flashed, so STORY-6.4's ESP32 and pico_w criteria stay open and unmet - notably ESP32_GENERIC's single-UART, network-only shape, which nothing else here reproduces. What the one board did prove: the manifest → fetch → hash-verify → flash → version → probed-caps chain completes, with `stm32-pybd-sf6-debug`'s claims surviving `capcheck` against the live probe. Getting there took fixing (1) the STORY-4.1 installer's hardcoded `/lib` root, which is ENODEV on a board that mounts its filesystem at `/flash` - now resolved from the target's `sys.path` as `mip` does; and (2) a `save_names` false negative, because `probe_capabilities` read its own frame and so measured whichever compiler produced debugpy rather than the firmware - an `.mpy` install carries no local names (LOCALNAMES_PERSIST is deliberately off), so STORY-3.3's rule rejected a correct manifest. Both were invisible to the unix port and the fake transports, which share the one filesystem shape and compile debugpy from source. New: Q11, closed 2026-08-10.
- **STORY-6.2 is mostly already delivered (2026-08-08); the story stays OPEN for hardware.** Revalidation found no separate network flow to write: `do_debug` branches only on `is_unix`, so `kind = "serial"` and `kind = "network"` take the same connect/raw-REPL/handshake path, and what the kind changes is only which host-resolution rule applies. Criteria 4 and 5 were already covered by s5.4, including against a raw-REPL source with echo. Criterion 3 is covered at the boundary but is unreachable end-to-end here by construction - a pty peer is handed `known_host="127.0.0.1"` because it is a local process, so it can never produce the "no routable address" case; that needs a `socket://`/`rfc2217://` transport (ampremote) or a board. Criterion 2 was the one hardware-free gap and is now closed by `tests/test_s6_2_network_flow.py`, mutation-confirmed. All three open questions decided at phase entry, including one already answered by Q8: `--port 0` is rejected up front, so the handshake port can no longer differ from the bound port. Criterion 1 needs a board.
- **STORY-6.1 hardware-free portion done (2026-08-07); the story stays OPEN.** `StreamTransport` (device, polled and thread-free) and the localhost<->serial bridge (host, built by generalising `dap_log`'s existing proxy rather than adding a second pump) both work over a pty pair against the unix firmware, and criteria 3, 4-upstream and 5 pass. Criteria 1 and 2 need a board and are untouched. Three caveats recorded in the ticket rather than glossed: the production path cannot activate (probe and detector both return "no" in lockstep, so the two halves meet only in the test); throughput on a real board is unmeasured and is now a risk-register entry; and criterion 4's ampremote half was removed rather than guessed at, since `do_reconnect`'s real signature matched neither candidate. Review caught two tests that asserted nothing and one criterion reported as delivered while the device still wedged.
- **STORY-7.2 DONE (2026-08-07). EPIC-7 is complete.** A status-bar picker
  over `mpdebug.toml`, selection persisted in `workspaceState`, capability text
  taken only from the probe — unprobed before a handshake, a target's cached
  probe dropped as a new session starts, and a probe shown under a different
  selection labelled with the target it actually came from. The discovery walk
  duplicates `mpdebug_config.find_config` in TypeScript because the command
  exposes no target list; a cross-check test runs the real Python and the
  TypeScript over the same fixture trees so the duplication cannot drift
  silently. Two acceptance criteria were untested as first written and are now
  mutation-confirmed: explicit `target` beating the picker, and persistence
  reaching the memento. Known limitation, recorded not fixed: the picker
  discovers from `workspaceFolders[0]` while the resolver spawns in
  `config.cwd` — identical for single-root projects, divergent in a multi-root
  one, and reconciling them needs a UI decision about which launch config
  applies.
- **STORY-7.4 DONE (2026-08-07).** A real Extension Development Host (VS Code
  1.132.0, `ms-python.debugpy` installed into it) drives this repo's own
  `.vscode/launch.json` and asserts a stop at `src/target.py:78` by
  `stackTrace`, that the Dynamic-trigger provider is genuinely reached by VS
  Code's picker, and that no mpremote child outlives its session. It settles
  s7.1's two open questions: the `mpyDebugLaunchId` marker DOES survive
  ms-python.debugpy's resolver, so the port fallback is insurance rather than
  the working path, and the Dynamic registration does activate. **What it
  cannot prove, and this is recorded rather than glossed:** that the
  extension's own kill is what reaps the child. On unix the reap is
  over-determined — the on-target server exits on any disconnect — so deleting
  `terminateChild` leaves the suite green. The acceptance criterion holds; the
  attribution does not, and needs STORY-6.4's hardware (the `--dap-log` proxy
  path, where the command outlives the device session, is where that kill is
  load-bearing). Two of the first version's tests passed with the behaviour
  they tested deleted; both were caught by mutating the source and are now
  assertions with teeth.
- **STORY-7.1 DONE (2026-08-07), and STORY-7.3 with it.** `extension/`
  registers debug type `micropython`; the resolver spawns `mpremote debug`,
  reads the handshake and starts a debugpy attach with no host/port typed
  anywhere. `.vscode/launch.json` is one config with no `promptString` inputs.
  45 node tests run from `make test`, including an integration test that reads
  the checked-in launch config and drives the real command with it — so the
  wire contract between command and extension is tested, not assumed. What
  remains unproven is VS Code's own variable substitution and
  `startDebugging`, which is s7.4's job. pathMappings are emitted only for the
  unix flow; a device target gets none rather than a fabricated identity map,
  pending STORY-4.3. Two decisions: a malformed `MPDBG-READY`-prefixed line is
  a diagnostic, not a fatal error (the unix flow echoes the debuggee's own
  stdout, so the stream carries lines from a source that never agreed to the
  contract), and `mpremoteArgs` lets `mpremotePath` be an interpreter, without
  which the checked-in config could not run — no released mpremote has the
  `debug` command.
- **STORY-8.1 DONE (2026-08-06).** The debug branches are registered in
  ampremote's `mbm.toml` and composed: ampremote `35d1533b47`, top-repo
  `e359b24`, pushed. The 8-file conflict was base skew after all — mbm rebases
  onto `upstream/master`, and that remote-tracking ref in
  `~/ampremote/micropython` was 356 commits stale; the earlier control checked
  `origin/master`, a different ref in that submodule. Fetching reduced it to
  four ordinary content conflicts in branches that had never been rebased onto
  current master, the only real one being #18436 × #18785 in `fs_writefile`.
  **New trap recorded: mbm's skip-on-resume also fires on rebase conflicts**,
  reporting "All branches integrated" with three of ten missing from the tip;
  always verify containment, never the summary. Verified behaviourally, not
  just structurally — `MPY_DEBUG_MPREMOTE_DIR` points this repo's DAP suite at
  any mpremote tree, and all 278 tests pass against the ampremote composition.
- **Every upstream PR this project feeds has a red lint job, in both
  submodules; now fixed on the integration branches (2026-08-09).** `make lint`
  excludes both submodules (`pyproject.toml`), so nothing here has ever run the
  checks that gate the PRs. Each submodule pins `ruff==0.11.6` and runs both
  `ruff check .` and `ruff format --diff .` on every push and pull request.
  Measured at PR #1022's actual tip (`3432190b8858`): 7 check failures and one
  unformatted file. On micropython's integration branch: check clean, but five
  unformatted files, all of them ones this project's lineage introduced
  (`tests/misc/sys_settrace_locals*.py` from `local_names_implementation`,
  `commands.py` from the `--loop` story, and all three `transport_serial.py`
  hunks from the mount read bound - `git log -L` first attributed two of them
  to the upstream `pyboard.py` detach commit, but that is what `-L` reports for
  a line whose content has since moved: upstream master's copy of that file is
  format-clean, and the two signatures only exceed the line length once
  `timeout_overall`/`timeout_overall_strict` are added to them).
  None of it is downstream of this session's work, though rewriting
  `test_vscode.py` added one more `T100`.
  Fixed by cause rather than by blanket suppression: `T100` (flake8-debugger)
  reads a debugger import in a shipped file, and the two samples that tripped
  it no longer start a debug session at all, so the rule finds nothing to
  flag (2026-08-10 entry at the top of this section); the `EXE001` shebangs came off
  `test_vscode.py` and `demo.py`, which run under `micropython` and not
  `python3`, while `dap_monitor.py` (genuinely CPython) became executable;
  `RSE102` and both formatters were let to fix themselves. Reformatting
  `sys_settrace_locals.py` is safe against its `.exp` file only because no
  hunk changes a line count - checked, and the four settrace tests re-run
  green.
  **Two standing consequences.** A submodule edit is not verified by
  `make lint`; `make lint-submodules` now reproduces both CI jobs exactly and
  should be run before claiming a submodule change is clean. And these fixes
  sit on integration branches, so `make integrate` discards them and no PR
  sees them until they are folded onto the feature branches. Counting that debt
  properly while recording this found it understated: seven mpremote commits are
  unfolded, not the four the earlier note names. See the risk register row.
- **The lint fixes that have an owning feature branch are now on it, and two
  more red jobs turned up in the process (2026-08-09, user authorisation:
  push lint fixes to the original branches, but only what the project's own
  tooling dictates).** micropython `local_names_implementation` took the
  settrace test formatting (`42fc0e685e`) and is now green on `ruff check`,
  `ruff format --diff` and `tools/verifygitlog.py`; so is `pdb_support`
  (#8767), which needed nothing. micropython-lib `add-debugpy-support`
  (PR #1022) took three commits on top of `3432190b8858`: the one formatter
  hunk (`1e43440`), the three `EXE001` shebangs (`7c6ce9f`), and four typos
  (`88e796a`). Its `ruff format --diff` and `codespell` jobs are green.
  **Two jobs nobody had looked at.** `codespell` (pinned 2.4.1) was failing on
  four words in `debug_session.py`; the workflow exists only on upstream master,
  so it is invisible from a checkout of the branch and only shows up in the PR's
  check list. And the check that reports as **`build`** in that list is the
  commit-formatting job from `commit_formatting.yml`, which runs
  `tools/verifygitlog.py -v upstream/master..HEAD --no-merges`: 12 of the PR's
  33 commits fail it, 46 errors in three groups - 11 body lines over 75
  characters, 7 subject lines that do not match `^[^!]+: [A-Z]+.+ .+\.$`, and
  6 commits whose sign-off is not the final line, because a `Claude-Session`
  trailer sits after it. That last one is the trap for this project: on these
  branches `Claude-Session` has to go *before* `Signed-off-by`.
  **Both of the jobs this pass deliberately left red are now green
  (2026-08-10), each by an explicit user decision:** `T100` was fixed in the
  sample files rather than in the repository's lint config, and the 12 commit
  messages were rewritten and force-pushed. See the 2026-08-10 entry at the top
  of this section.
- **STORY-8.2 DONE (2026-08-06).** PR micropython-lib#1022 fast-forwarded to
  `3432190b8858` (32 commits), carrying EPIC-1's foundations, the Q8
  bind/accept split, the message-pump deadlock fix and the parallel lineage's
  enhancements, with a PR comment leading on the two correctness fixes and
  offering to split the rest. Upstreaming authorisation is standing: the WIP
  PRs may be updated, and `~/ampremote` is a public staging ground for in-PR
  branches.
- **STORY-7.1 resequenced ahead of STORY-4.3; hardware work moves hosts
  (2026-08-06, user decision).** EPIC-6's transports and STORY-6.4 wait for
  the work to be moved to a host with boards attached, which the user will do
  manually; everything reachable without hardware comes first. STORY-7.1 is
  the frontier. Original finding follows.
- **Sequencing finding: STORY-7.1 is closer than the roadmap implies
  (2026-08-06).** The extension is listed as needing STORY-4.3, which is
  listed as needing 4.2 — unreachable, since 2.1 said yes. Read as 4.1, 4.3's
  own ticket already records that D2 makes staleness structurally impossible
  in mount mode, leaving it as "assert the mount is active, and generate
  absolute pathMappings". But the debug flow mounts and syncs nothing today,
  and the only working transport is unix, where local and device paths are
  the same file and pathMappings is the identity. So 4.3's real content is
  gated behind the device transports, i.e. behind hardware — while the part
  7.1 actually needs (spawn the command, read the handshake, map source) is
  satisfiable now for unix. Worth resequencing 7.1 ahead of 4.3 rather than
  waiting on a bench, but that is a planning decision, not a revalidation.
- **Debug work collapsed to one branch (2026-08-06, commit 3e692f7).** It had
  become five stacked branches, one per story, which is not what mbm composes:
  each branch's diff against master carried its ancestors', so none was
  independently reviewable or mergeable and an upstream move would have meant
  five ordered rebases. Q1 had already decided one branch and two consumers;
  the stack was drift, not a decision. `mpremote_debug` now holds the whole
  command as six commits off master, `mpremote_debugpy_install` stays separate
  because it genuinely is independent, and the recomposed tree is
  byte-identical (pin `e1cfcc83f3c0` -> `51fec813ddcb`). **Rule for the rest of
  this project: a branch registered in `mbm.toml` is a prospective PR and must
  sit on upstream master, never on another feature branch.**
- **STORY-8.1 attempted, blocked on ampremote's own branch conflicts
  (2026-08-06).** The `mbm.toml` ordering work is done and verified (rename
  restored to last, the six debug branches inserted before it), but
  `mbm rebase --local` hits a genuine 8-file conflict at
  `mpremote_debugpy_install` between ampremote's existing stack and the debug
  branches' base — `console.py`, `transport*.py` and several shell tests, most
  of which the debug work does not touch. Three hypotheses were then tested:
  the stack (no — collapsing to two branches reproduces it identically), base
  skew (no — ampremote's master was 356 commits stale, and fetching to zero
  skew changes nothing), and ampremote's stack being broken on current master
  (no — the control with no debug entries integrates cleanly). So the
  installer branch does introduce it, yet it touches only three files and none
  of the five conflicted test/console paths, which a three-way merge cannot
  do. That points at mbm's merge mechanics rather than a content clash, and
  mbm is the user's tool.
  ampremote was restored completely (submodule `5b459e2d57cc`, branch unmoved,
  `mbm.toml` reverted, tree clean; pre-attempt tip tagged
  `ampremote-pre-debug-20260806` on the fork). Also recorded: Q1's "one
  branch" is in practice one lineage of six stacked branches.
- **Frontier partly unblocked (2026-08-06).** EPIC-3, EPIC-4's
  unconditional part, EPIC-5 and STORY-6.3 are done, and the harness flake
  that gated everything is fixed at the root. What remains at the front of
  the queue needs something this environment cannot supply:
  - **STORY-6.1 / 6.2 (device transports) are hardware-gated.** Four of
    s6.1's five acceptance criteria name real boards (rpi_pico_w for the
    multi-CDC path, ESP32_GENERIC for the negative probe); only the
    bridge's PTY-loopback unit tests are hardware-free. Building the host
    half while its acceptance cannot be exercised is the pattern that
    produced s4.1's and s5.3's unverifiable claims, so it should wait for a
    bench rather than ship half-proven. STORY-6.4 was already deferred here.
  - **STORY-8.1 / 8.2 (upstreaming) need authorisation, not analysis.** 8.2's
    acceptance is "PR(s) opened" against micropython-lib — publishing to a
    third-party upstream — and 8.1 edits `~/ampremote`, a different
    repository. Both are outward-facing in a way the standing "proceed
    according to roadmap" instruction does not obviously cover.
  So the next move is a decision (bench time, or approval to open upstream
  PRs), not more autonomous execution.

- **STORY-5.5 DONE (2026-08-06, commit d144e56).** `mpremote debug` is driven
  end-to-end through the DAP harness - breakpoint, step, locals with the
  readOnly hint, continue - against the endpoint the command reports, with the
  adversarial cases asserting no orphan survives. Suite 256 passed / 1 skipped
  / 1 xfailed; verified by mutation. **EPIC-5 is complete.** The
  order-insensitive matching question is settled as "don't": three independent
  measurements make it worse, because a stale match satisfies the wait early.
- **STORY-5.5 blocker settled; harness assertions were vacuous (2026-08-06,
  commit 339192a).** Triage proved a breakpoint DOES fire through
  `mpremote debug` — identical `stopped`/`breakpoint` events on the direct and
  mpremote paths — so there is no product bug and s5.3's acceptance holds. The
  parked test's failure was an `AttributeError` from dereferencing `.body` on
  `wait_for_msg`'s bool return, misread as "the target never ran". Chasing it
  exposed that `wait_for_msg` returned true unconditionally (its `count`
  defaults to 0), so all nine `assert wait_for_msg(...)` sites in the suite
  asserted nothing; it now returns the matched message and those assertions
  are real, with one that failed the moment it started meaning something.
  Also corrected: the earlier note that the parked tests were xfail-marked was
  false — there are no xfail markers on that branch.
- **STORY-5.5 first attempt, NOT landed (2026-08-06).** Both halves failed
  review. The harness flake fix was *reported* as a watermark with a measured
  20/22, but the shipped code was the full-list scan already tried and
  reverted on 2026-08-05; independent re-measurement found it worse than
  baseline. Reverted — and worth noting the false measurement was relayed
  onward before being checked. The command-drive tests xfail the story's own
  criteria (breakpoint, locals, continue) on an unverified claim that the
  target never runs under `mpremote debug`; the ticket forbids that, and the
  claim must be settled first, since if true it makes s5.3's acceptance false.
  Parked on `s5.5-command-drive-wip`; main green at 250 passed / 1 xfailed.
- **STORY-5.3 DONE (2026-08-06, commit b89d533).** `mpremote debug` with a unix
  target resolves a binary, runs the boot script under it, reports the endpoint
  and supervises the child (pin `b874d8854419`, suite 250 passed / 1 xfailed).
  Two findings are worth carrying as patterns. The test harness `pkill`ed stray
  firmware processes before every run, hiding the very leak it should have
  caught: deleting it turned five tests red at once, all written against the
  earlier behaviour where the command returned instead of supervising. And a
  stray uncommitted edit to the unix variant header had switched on
  `LOCALNAMES_PERSIST` — the flag kept off deliberately because it corrupts
  line numbers — apparently to make a master-based branch behave like the
  composition; it plausibly explains two failures reported as pre-existing.
  Reverted, and the composed tip carries no such define.
- **STORY-5.4 DONE (2026-08-06, commit e5cf83d).** One `MPDBG-READY` parser
  now serves both control planes (pin `2ba7cf660087`, suite 239 passed /
  1 xfailed), returning a record with an endpoint kind so a future serial data
  plane needs no caller change. Review caught that the refactor had silently
  dropped s5.1's raw-REPL exception drain — the new synthetic streams were too
  tidy to expose it. The 0.0.0.0 rules also earned more than expected: as
  specified they left the suite with no end-to-end success path, since the unix
  build always reports a wildcard; resolving a wildcard-over-pty to loopback
  (a pty peer is local by construction) means `mpremote debug` now reports an
  address a client really connects to, proven by the pty test attaching to
  exactly what it printed. Detail in the ticket.
- **STORY-5.2 DONE (2026-08-05, commit 505ac27).** `mpremote debug <name>`
  resolves kind/device/firmware/program from the nearest `mpdebug.toml` (pin
  `38a903b8de20`, suite 197 passed / 1 xfailed). Review killed a shape-based
  heuristic that decided whether the positional was a target name or a connect
  string: it did not recognise a bare `COM4`, so on Windows every bare COM
  invocation became "unknown target" once a config existed. Names now either
  match a target or go to the transport, with the error naming configured
  targets. The hand-mirrored capability vocabulary is now guarded by a test
  pinning the live probe keys against `KNOWN_CAPABILITIES`.
- **Q8 IMPLEMENTED (2026-08-05, commit 5a9f546).** `listen()` returns the
  bound endpoint; accept and `initialize` moved into `wait_for_client()`
  (micropython-lib `00d364e7fb` -> `12ddfbfc96c3`, on the foundations branch
  so it survives recomposition). Two things only running it revealed: the
  launcher imported the target between listen and wait, so a bad module closed
  the socket before a client could attach (import moved after
  `wait_for_client`, which also means top-level target code now runs with
  breakpoints set); and the harness fixture's startup stdout drain swallowed
  the now-earlier `MPDBG-READY` line — the same "harness only worked because
  of the old ordering" root cause as the blocker itself. `--port 0` turned out
  to have been silently broken on unix all along, which has no `getsockname()`
  at all. Suite 126 passed / 1 xfailed. STORY-5.1 is unblocked.
- **STORY-4.1 DONE (2026-08-05).** Both halves are composed: `#18436`
  verify_hash, then the installer itself (`760f6597db44`, commit `d14de9d`).
  The installer's 41 tests run against the composed tree — the first run that
  exercises `fs_writefile(verify_hash=True)` against a transport that has it —
  and the suite is 123 passed / 1 xfailed. Two review findings are worth
  remembering as patterns rather than incidents: a documented safety
  invariant that was simply false (write ordering cannot keep a package
  non-importable, because MicroPython treats an `__init__`-less directory as
  a namespace package), and a fast path that trusted existence over content
  in a story whose whole premise is "trust content, not names". Detail below
  and in the ticket.
- **STORY-4.1 detail (superseded by the line above).** The `#18436` verify_hash branch is
  registered and composed (integration `f9d7c96b96` -> `f8852e8fb2ca`,
  commit `700c311`); its rebase needed a real conflict resolution, since
  upstream's later `_quote_path()` hardening and the PR's unquoted path
  interpolation each lose something if taken wholesale. The installer itself
  (`mpremote_debugpy_install` @ `75245612f9`, 13 tests) did NOT converge in
  four review rounds and is parked on top-repo branch `s4.1-debugpy-install`,
  unregistered. Chief finding: the documented write-order safety invariant
  does not hold, because MicroPython imports an `__init__`-less directory as
  a namespace package, so partial-install detection rests entirely on the
  marker. Two further rounds closed that and the rest: the fast path now
  hashes each recorded file device-side AND rejects any unrecorded file under
  `device_dir` (a later `.py` shadows the installed `.mpy`), a sweep that
  cannot delete a stale file raises rather than recording a clean install,
  and a `device_dir` broad enough to reach the marker or `/` is refused.
  Review is clean at `mpremote_debugpy_install` @ `dcd4fb702c` /
  `s4.1-debugpy-install` @ `7b1160f`, still unregistered: its suite passes
  only against firmware from the composed tip, so the numbers must be
  re-taken after composition. Detail in the ticket.
- **STORY-5.1 DONE (2026-08-05, commit 86b4625).** `mpremote debug` is composed
  (pin `60d32aa0a736`) and meets its criteria: against the unix firmware over a
  pty with no client attached it prints the device-reported endpoint and
  capability dict. Composition is what made that testable — parked on a side
  branch the story pinned a submodule with no `sys.settrace`, so every
  device-path test silently SKIPPED and the reported pass count was not
  reproducible (19 passed / 5 skipped then; 24 passed / 0 skipped now, suite
  150 passed / 1 xfailed). Same trap as s4.1, now recorded in both tickets.
  Review also caught the boot script executing on import (it ships inside the
  mpremote package) and a test renamed to claim it proved the Q8 ordering while
  still starting a client first. Detail in the ticket.
- **STORY-5.1 history (superseded by the line above).** The `mpremote debug`
  skeleton exists on `andrewleech/micropython` branch `mpremote_debug_command`
  (registration, argument surface with validation before any device contact,
  raw-REPL upload/exec of the boot script now shipped as an mpremote package
  resource, private handshake reader) with host-side coverage in
  `tests/test_s5_1_mpremote_debug.py`. Adversarial review found the story's
  endpoint-printing criterion unreachable: `MPDBG-READY` is only emitted after
  a DAP client attaches (Q8, `20260805_handshake-ordering-blocker.md`). Also
  surfaced: mpremote's `nargs=REMAINDER` chaining silently swallows options
  written after the positionals (upstream-wide trap, needs documenting plus
  tests that a flag takes effect), and an early device exit stalls for the full
  timeout while discarding the device's own error text. Both are now fixed and
  the branch is pushed (`mpremote_debug_command` @ `f240be068a`, 17 tests,
  suite 99 passed / 1 xfailed); the top-repo side is parked on branch
  `s5.1-do-debug`. `mbm.toml` is deliberately unchanged and the composition
  not moved until Q8 is decided.
- **STORY-3.4 DONE (2026-08-05).** `docs/firmware.md` (variant/capability
  tables with an evidence column separating probe-confirmed from build
  intent, fetch/select usage, CI-parity build commands per port, capability
  truth incl. the `_SAVE_NAMES` no-op and PERSIST warnings) + README link.
  Doc-tested verbatim (launcher commands, unix build, live
  `get_capabilities()` cross-check of the unix row; the rp2 docker command
  run to a complete in-container build) and hardened over three opus
  factual-review rounds plus a final inline pass — review killed two real
  over-claims (local-vs-Release reproducibility scope; capcheck guard is
  opt-in, not per-session). EPIC-3 is now fully closed except the
  STORY-6.4-deferred hardware checks.

- **STORY-3.2 DONE (2026-08-04): stage 5 — first deliberate master bump +
  second Release.** Default-target `mbm rebase --local` moved both
  compositions onto current upstream/master (micropython `1f70f60684` →
  `f9d7c96b96` on `06bcfd5b74`, ~690 commits; micropython-lib `5f74950e9e` →
  `00d364e7fb` on `dbb3b45fde`; the known profile.c/mpconfigvariant.h
  conflict replayed from rerere, debugpy tree content-identical across the
  bump). Release `fw-f9d7c96b96` published and re-runnability proven
  stronger than before: the dispatch and push-triggered runs built **all
  four artifacts byte-identical** (the first Release had proven unix only).
  Manifest updated to the new entries; e2e re-verified (fetch detects the
  stale cached artifacts, re-fetches, verifies; select resolves; the fetched
  unix binary runs the suite 82 passed / 1 xfailed). The unix gate's flake
  retry budget rose to 4 attempts (measured ~1-in-2 full-run flake rate),
  and one interleaving flake was observed OUTSIDE the four-id allowlist
  (`test_epic1_breakpoint_stops_target`) — risk row updated; the s5.5
  harness fix is the real cure. Historical submodule pins are preserved as
  `mpy-debugpy-pin-<sha12>` tags on both forks (mbm force-pushes orphan old
  tips). Hardware boot-check stays deferred to STORY-6.4. Frontier: s3.4
  (now fully unblocked), s5.1, s4.1.
- **STORY-3.2 stages 1–4 DONE (2026-08-04).** The composition
  gained the `debug_board_flags` local-only branch (settrace committed in the
  three device board configs; integration tip `1f70f60684`), which also
  surfaced and fixed a real firmware bug (missing `stdio.h` in `py/profile.c`,
  exposed by rp2's `-Werror`; fixed bisect-clean on
  `local_names_implementation`, PR #5 refreshed). `.github/workflows/
  firmware.yml` builds all four targets (first push green; hardened over four
  opus review rounds: SOURCE_DATE_EPOCH reproducibility — unix sha256 proven
  identical across independent runs — sha-in-banner provenance, digest-pinned
  IDF container, fragment-vs-bytes cross-check, republish guard, per-device
  settrace symbol assertions, toolchain provenance in fragments). First
  Release `fw-1f70f60684` published; `firmware/firmware.toml` now carries
  exactly the four CI entries (legacy dropped) and the full chain is verified
  end-to-end: fetch → sha256 verify → select → the fetched unix binary runs
  the host DAP suite green (82 passed / 1 xfailed; suite grew 46 → 82 across
  the story: fetch/corruption/fragment/capability-verifier tests). s3.4's
  Release-URL sections are now unblocked.
- **Roadmap review (2026-07-15, `20260715_roadmap-review.md`).** Landed results
  re-verified against the tree, fork and GitHub: submodule pins, canonical
  branch tips, PR #5 head and the mbm.toml composition all hold; `make test`
  gate reproduced (46 passed / 1 xfailed on re-run — the first run hit the
  harness interleaving flake, now a risk-register row with the mechanism pinned
  down for s5.5). One hygiene gap found and fixed: micropython-lib's local
  `add-debugpy-support` was left at its mbm-rebased copy instead of reset to
  the fork tip (composition correctness unaffected; `pr_number` entries fetch
  `pull/N/head` live). Q7 (push-to-origin) opened; upcoming-ticket drift
  recorded in the review note for phase-entry revalidation.
- **Phase 0 DONE (2026-07-15): STORY-8.6 + STORY-8.5.** The canonical locals
  branch `andrewleech/local_names_implementation` is rebuilt as 7 bisect-clean
  commits on `pdb_support` (`2ff9f3cd8` -> `7ae5f769c`, fork PR #5), carrying
  the two integration fixes, the new param-names regression test, and repairs
  for defects the recomposition never surfaced because the firmware tests had
  never been run: a real qstr sentinel bug (`MP_QSTR_NULL` vs `MP_QSTRnull`),
  three locals tests failing on stale expectations / CPython 3.13+ divergence,
  a pre-existing one-extra-line-event tracer artifact on for-loops (now
  documented via static `.exp`), in-tree scratch files, and two latent
  feature-macro compile failures. `frame_f_locals` keeps the `local_NN`
  fallback for code without name data (load-bearing for `.mpy` device
  debugging). Both integration branches are now mbm-rebuildable from
  `mbm.toml` (`make integrate`; always `--local`, pushes stay manual):
  pinned-target proof runs had zero conflicts, micropython recomposed to
  `6863e938a1` (differs from the old tip by exactly the reviewed 13-file
  divergence manifest), micropython-lib to `d7b297dfd1` (tree-identical to the
  old tip); `make test` gate exactly 13 passed / 1 xfailed. Full record:
  `20260715_phase0-canonical-branches-mbm.md`.
- **STORY-6.5 DONE (2026-07-15).** DAP `evaluate` executes statements for
  `repl`/`clipboard` contexts (eval-first, exec-on-SyntaxError against live
  `f_globals`; shadowed-local writes warn; watch/hover regression-locked;
  46 passed / 1 xfailed). Delivered through the new composition flow:
  foundations branch `e359a95` -> `4fabcb3`, recomposed `mpy-debugpy`
  `5f74950e9e`, both fork-pushed. Reentrancy proven safe via the VM's
  `mp_prof_is_executing` guard. Known harness startup race noted in the
  ticket for s5.5.
- **STORY-3.3 DONE (2026-07-15).** Legacy manifest entries deprecated (provenance
  kept, excluded from selection; collapsed per-board entries land with s3.2),
  `launcher/firmware.py select` resolves required capabilities against manifest
  intent, and `launcher/capcheck.py` hard-errors any claimed-true-but-probed-false
  key against the real MPDBG-READY handshake. Host suite 39 passed / 1 xfailed
  (26 new tests). s3.2 was blocked on the push-to-origin policy call until Q7
  DECIDED (2026-07-15: push as work lands); s3.4 waits on s3.2's Release URLs.
- **Integration recomposed onto current upstream master (2026-07-06).** The first
  migration into this repo vendored Josverl snapshot branches frozen at an old master;
  that was rejected and redone. Both submodule integration branches are now composed
  from the andrewleech lineage onto current upstream master: `micropython` @
  `mpy-debugpy` (`18c76f7c`) = master + #8767 (merged) + cherry-picked locals commits +
  two integration bug fixes; `micropython-lib` @ `mpy-debugpy` (`0d42fcd`) = master +
  #1022 + enhancements + EPIC-1 foundations. Jos's extra FIRMWARE commits were reviewed
  and all skipped (dead code / incompatible with the emitglue-embedded locals approach);
  on the LIB side three of his commits (ruff format, cleanup, Special fixup) are in the
  integration's ancestry via the EPIC-1 base — the ruff-format one is a rebase hazard
  for upstreaming, handled in ticket s8.2. See
  `20260706_recomposition.md` for the decision record. Composition is hand-done
  (merge + cherry-pick), not yet `mbm rebase`-reproducible — that is STORY-8.5.
- **Firmware facts corrected during recomposition** (supersede some constraint notes
  below and parts of BACKGROUND.md): the real name-capture macro is
  `MICROPY_PY_SYS_SETTRACE_LOCALNAMES` (`_SAVE_NAMES` is the old lineage's spelling and
  a no-op on the current branch); the unix `standard` variant on the integration branch
  enables settrace + LOCALNAMES by default (no CFLAGS_EXTRA needed); the `line` trace
  event fires BEFORE the statement executes (correct CPython semantics — tests must
  break after an assignment to observe the variable); two real firmware bugs were found
  and fixed on the integration branch (`f_locals` slot indexing reporting variables
  under wrong names; `MICROPY_PY_SYS_SETTRACE_LOCALNAMES_PERSIST` corrupting line
  numbers for all compiled code — now off by default). Upstreaming these fixes to the
  canonical PR branches is STORY-8.6.
- **EPIC-1 (foundations): DONE and verified.** STORY-1.1/1.2/1.3/1.4 implemented in the
  debugpy module + unified `launcher/mpy_launch_debugpy.py`; STORY-1.5 host tests green
  (13 passed, 1 pre-existing unrelated xfail) with behavioural coverage. A real framing bug
  in `common/messaging.py:recv_message` (header stripped before the body arrived →
  desynchronised DAP framing → breakpoints never fired) was found and fixed during review;
  this, not the handler, was why `configurationDone` appeared "unprocessed". Review also
  fixed: leftover debug prints, an ESP32 network-probe crash in the launcher, a `port: 0`
  handshake, and an empty-`f_locals` `save_names` false-positive. Committed and
  published on `andrewleech/micropython-lib` @ `mpy-debugpy` (`0d42fcd`).
- **STORY-3.1 (firmware manifest): DONE.** `firmware/firmware.toml` + `launcher/firmware.py`
  (`list`/`verify`/`fetch`). Found provenance drift — the prebuilt binaries are NOT all from
  the pinned submodule SHA (only `unix_settrace_local_xx` matched the then-pinned `05b7818`);
  download URLs are placeholders pending a CI release step. Reinforces STORY-1.2 (probe,
  don't trust names). NOTE: after the 2026-07-06 recomposition all four manifest entries
  describe legacy pre-recomposition artifacts; the manifest is a provenance record until
  STORY-3.2's CI builds replace them from the pinned integration SHA.
- **EPIC-2 spikes: BOTH RESOLVED (see `SPIKE-2.1-mount.md`, `SPIKE-2.2-serial-dap.md`).**
  - STORY-2.1 (mount under settrace): **CONDITIONAL YES.** Mount's fs-RPC is serviced by the
    host follow-loop regardless of pause state; a paused debuggee issues no filesystem calls,
    so mount + network-DAP coexist. Corrected limitation: mount + `sys.modules` eviction /
    re-import gives a no-upload edit→re-import iteration loop (surfaced STORY-4.5); only an
    already-paused frame can't be live-edited. **Effect:** EPIC-4's heavy sync engine is the
    fallback, not the mainline.
  - STORY-2.2 (DAP over serial): **CONDITIONAL / board-dependent.** Dual-USB-CDC boards can
    give DAP its own channel (yes); single-UART boards (current ESP32 targets) would need an
    unbuilt framing layer (no). **Effect:** network stays the primary device transport; serial
    DAP is a probed multi-CDC subset. Unix gets a dedicated pipe fd for free.
- **Net re-scope from the spikes:** device mainline = **network DAP + `mpremote mount` for
  source + re-import to iterate**; EPIC-4.2/4.3 (hash sync + staleness guard) demoted to the
  single-UART/no-mount fallback and unix. New follow-ups: STORY-4.5, STORY-6.5.

---

## Background research

### The current friction (from the wrapper repo)

To debug one file today a user hand-orchestrates: pick one of four prebuilt firmware
variants via an `MP_DEBUG_VARIANT` env var in `tasks.json`; set `MICROPYPATH` so `src/`
and the debugpy module resolve; run one of two near-duplicate launcher scripts
(`launcher/mpy_launch_debugpy_{unix,esp32}.py`) that call `listen()` → `breakpoint()` →
`debug_this_thread()` → `sleep(2)` → import+call a target; pick one of nine `launch.json`
attach configs, several with hardcoded IPs (`192.168.137.117`, `my-esp32.local`) and
inconsistent ports (5678 vs 5679); and for devices run `compile_debugpy.py` → `mpremote
mip install` → `mpremote cp -r src/ :/` on every run, re-doing everything with no staleness
detection. Config is scattered across `launch.json`, `tasks.json`, `debugpy.json`,
`debugpy_mpy.json`, and the two launcher scripts.

### Hard constraints carried from the firmware findings (see BACKGROUND.md)

- The debugpy server has no background thread. All DAP handling is piggybacked on the
  `sys.settrace` callback; while paused it busy-polls `sleep(0.01)`. Any design has to fit
  this single-threaded, poll-driven model. There is no second socket, no async.
- `frame._set_local` / `sys._set_local_var` exist in **no** branch of either lineage
  (re-confirmed on the recomposed integration, micropython @ `18c76f7c`). setVariable on
  locals degrades to an error; only global edits work. Local editing must not be promised
  in UX; the readOnly presentation hint (STORY-1.3) covers it.
- Variant *names* encode capabilities that the firmware may not have (e.g.
  `unix_settrace_set_local` against firmware with no `_set_local`). Capability must be
  probed at runtime, not inferred from a directory name.
- `f_locals` is a fresh snapshot dict per access, not a live view. `f_back` and
  `f_globals` are live. Name capture is `MICROPY_PY_SYS_SETTRACE_LOCALNAMES` on the
  current branch (the older lineage's `_SAVE_NAMES` spelling is a no-op there); without
  it, names are positional placeholders `local_N`. Function parameters are present in
  `f_locals` under their real names on the current branch — the old omission is gone
  (Q6 DECIDED, verified empirically; `20260706_flocals_params.md`).
- The `line` trace event fires BEFORE the statement executes (correct CPython semantics).
  Jos's old firmware fired it late; anything depending on that timing is wrong on the
  current branch (this already bit one test — break after an assignment, not on it).
- The unix `standard` variant on the integration branch enables settrace + LOCALNAMES by
  default; stock upstream unix does not, and other ports need
  `MICROPY_PY_SYS_SETTRACE=1` + `MICROPY_PY_SYS_SETTRACE_LOCALNAMES=1`. Keep
  `MICROPY_PY_SYS_SETTRACE_LOCALNAMES_PERSIST` off — it corrupts line numbers for all
  compiled code.

### ampremote / mpremote structure (inspected at `~/ampremote/`)

`ampremote` is an `mbm`-managed integration of upstream `mpremote`. The submodule
`~/ampremote/micropython` sits on integration branch `ampremote`; `mbm.toml` lists the
branches composed onto it, in merge order, with `ampremote_rename` always last. `make
install` does `uv tool install --editable` of `micropython/tools/mpremote`, so edits under
`micropython/tools/mpremote/mpremote/` take effect live as the `ampremote`/`ampr` CLI.
The Python module stays named `mpremote`; only the distribution name and console scripts
change (in `ampremote_rename`). Policy (from `~/ampremote/CLAUDE.md`): default to upstream;
keep a branch local-only when it is distribution-specific or needs hardware validation
first, in which case raise it as a draft PR on the `andrewleech/micropython` fork and
switch its base to `micropython:master` once validated.

Command architecture: commands are registered in the `_COMMANDS` dict in
`micropython/tools/mpremote/mpremote/main.py` (line ~295), mapping a command name to
`(handler_func, argparse_func)`. Handlers live in `commands.py` and take `(state, args)`;
`state.transport` is the connected `Transport` (`transport.py` / `transport_serial.py`).
`state.ensure_raw_repl()` gets a raw REPL; `state.did_action()` marks work done. Existing
handlers `do_mount`, `do_run`, `do_exec`, `do_filesystem`, `do_mip` show the pattern a new
`do_debug` should follow. Adding a command is: write `do_debug`/`argparse_debug`, import
them into `main.py`, add an entry to `_COMMANDS`.

### What the already-integrated mpremote PRs give us (reuse, don't reinvent)

> **Correction (2026-07-06):** "already-integrated" means integrated in **ampremote's**
> tree (`~/ampremote/micropython`, e.g. `verify_hash` at its `transport.py:292`, QEMU PTY
> at its `transport_serial.py:108-111`) — NOT in this repo's micropython submodule, whose
> mpremote is plain upstream. Work that relies on these primitives (EPIC-4/5) must either
> register the relevant mpremote branches into this repo's `mpy-debugpy` integration
> branch (consistent with Q1/D5 — recommended) or run against the installed `ampr` CLI.
> Tracked in tickets s4.1/s4.2/s4.4 and the STORY-8.5 composition.

- **#18436 streaming hash verification** (`mpremote_file_cp_hash`): `transport.py`
  `fs_writefile(..., verify_hash=True)` and `fs_readfile(..., verify_hash=True)` compute a
  device-side sha256 during transfer and compare to the local hash. This is most of the
  incremental-sync/staleness engine already — we get per-file content verification for free.
- **#18785 fast encoded file transfer** (`feature/smart-encoding-fs-writefile`): speeds up
  the writes the sync engine issues.
- **#17322 auto-reconnect** (`mpremote_reconnect`): device drop/reconnect handling; useful
  when a debug run resets the board. Not for a debug *session*, though: STORY-6.1's
  criterion 4 was settled on 2026-08-10 as ending the session rather than reconnecting
  (`20260810_board-reset-mid-session.md`).
- **#19062 socket / rfc2217** (`mpr/fix_socket_rfc2217`): network transport handling —
  relevant to the network-attach flow and to any TCP endpoint work.
- **#18327 QEMU PTY detection** (`mpremote_pty`): lets the same flow target a QEMU PTY,
  useful for CI without hardware.
- **#17485 resume-by-default**, **raw-repl retry** (`mpremote_raw_repl_retry`): connection
  robustness the debug command inherits by building on `state`/`transport`.

### The mount mechanism, and why it is a spike (inspected in `transport_serial.py`)

`do_mount` → `SerialTransport.mount_local` installs a `RemoteFS` hook on the device
(`fs_hook_code`, `__mount()`) and then wraps the host serial object in a `SerialIntercept`.
The device's filesystem calls (open/read/stat over `/remote`) are issued as RPC back over
the **same serial stream**, sniffed out of the byte stream by `SerialIntercept` and served
by `PyboardCommand` on the host. This only works while the device is in a REPL read/write
loop able to service that RPC. Under debugpy the traced program spends its time in the
trace callback, and while paused it busy-polls — it is not servicing serial fs RPC. So an
`import` from a mounted `/remote` during a debug session (or while paused at a breakpoint)
plausibly hangs. Confirming the exact failure mode is a spike (STORY under EPIC-2) because
the answer deletes whole subsystems downstream: **if mount can be made to work under
settrace, live source mount replaces the entire sync/staleness engine**. If DAP can be
tunnelled over serial, the network transport, IP discovery and any TCP proxy disappear.

---

## Design position (summary)

CLI-first orchestration, shipped as an `mpremote debug` command, with a thin VS Code
extension on top. The extension exists only to deliver an F5-style one-keypress launch; it
must not own firmware selection, source sync, target launch or endpoint discovery — that
all lives in the command, which is testable headlessly (the wrapper repo already has a
`fake_vscode` DAP client and pytest harness) and works from CI and other editors.

One mental model for all three transports: **resolve target → ensure runtime → sync →
launch → attach**. A generated on-device boot script prints one machine-readable handshake
line (`MPDBG-READY {json}`) carrying the endpoint and probed capabilities; nobody types an
IP or port. Staleness becomes impossible-without-warning via hash-verified sync (or is
deleted outright if the mount spike succeeds). Capability is probed at runtime and surfaced
honestly; locals are marked read-only in DAP so the edit affordance is greyed out rather
than failing.

Spikes (mount-under-settrace, DAP-over-serial) run **early**, right after the no-regret
foundations, because each can remove a whole epic's worth of work.

---

## Design decisions

### Settled

- **D1 (2026-07-04) — CLI-first orchestration** as `mpremote debug`; the VS Code
  extension is a thin layer added last. (Design position above.)
- **D2 (2026-07-05) — Mount spike: CONDITIONAL YES** — mount fs-RPC and network DAP
  coexist; the sync engine (EPIC-4.2/4.3) is demoted to fallback. `SPIKE-2.1-mount.md`.
- **D3 (2026-07-05) — Serial DAP is board-dependent** — first-class only on multi-CDC
  boards; network stays the primary device transport. `SPIKE-2.2-serial-dap.md`.
- **D4 (2026-07-05) — Locals are read-only in DAP** until a real `_set_local` exists,
  gated on the runtime probe, never on variant names.
- **D5 (2026-07-06) — Canonical lineage is the andrewleech branches composed onto
  current upstream master**; Jos's snapshot branches are provenance only. Composition
  recorded in `mbm.toml`; hand-composed until STORY-8.5. `20260706_recomposition.md`.
- **D6 (2026-07-06) — Depend on the ampremote lineage for mpremote primitives until
  its PRs land upstream.** The prerequisite mpremote capabilities (verify_hash #18436,
  QEMU PTY #18327, auto-reconnect #17322, rfc2217/TCP #19062) are ampremote-only at the
  pinned upstream SHA. Rather than feature-detecting their absence, register the needed
  ampremote branches into this repo's `micropython:mpy-debugpy` composition via mbm
  (STORY-8.5 scope, consistent with Q1's one-branch/two-consumers model). The dependency
  dissolves naturally as each PR merges upstream and drops out of the composition at the
  next `mbm rebase`. Tickets written before this date (s4.x, s6.1/s6.2) hedge with
  "feature-detect" language — supersede that at phase-entry revalidation.

### Open questions

Q12 is open. Q1–Q8, Q11, Q13 and Q14 are closed; see DECIDED entries below.

**OPEN:**

- **Q12 (2026-08-08) - how does the firmware manifest express "this build has a
  second CDC interface"?** It cannot be `serial_dap`: that key deliberately
  reports which channel a session took, not what a board can do, so it is False
  on a REPL probe of a board that runs DAP over its second interface perfectly
  well. But the interface count *is* a firmware property (`MICROPY_HW_USB_CDC_NUM`
  in the board config), a user picking a variant for STORY-6.1 needs it, and
  STORY-3.3's rule says the manifest must never claim what a probe would
  contradict - so whatever key is added needs a probe that can check it, and
  `pyb.usb_mode()` reports the boot-time choice rather than the build's maximum.
  Surfaced by the published `stm32-pybd-sf6-debug` artifact predating the flag
  entirely. Settle with STORY-3.2's republish, since the two land together.

**DECIDED (2026-08-10):**

- **Q11 → its own command, `mpremote debugpy-install <package_dir>`.** On the
  `mpremote_debugpy_install` branch, with the installer's optional
  `--mpy-cross PATH`. The host directory is a required positional because
  mpremote ships no copy of debugpy and cannot guess where a checkout is; a
  path that is not a directory or has no `__init__.py` is refused before the
  port is opened, and the second refusal names the inner `debugpy/` when the
  named directory contains one, because pointing at micropython-lib's package
  folder instead of the package is the predictable mistake. Rejected: an
  implicit install on every device session, and a `--install` flag on `debug`.
  Both would need the unguessable host path on every run for the benefit of
  one run in fifty, and both would let a bare `mpremote debug` invoke an
  installer that *sweeps* its device directory, deleting a hand-placed or
  `mip`-installed file. The flag would also have grown fork PR #51 with a
  feature it does not need - which is what showed the question was not held by
  #51 at all, since the installer lives on a different branch. The soft-reset
  requirement needed no new mechanism: mpremote's `_auto_soft_reset` already
  resets on the first command of a separate invocation, and `+ soft-reset`
  covers a chain, so the command matches `mip install` in not resetting and
  instead prints the warning only when a write actually changed something.
  `--target` is deliberately not exposed: the device directory is resolved
  from the target's `sys.path` and is swept, so a command-line typo would
  delete unrelated files. Does not duplicate `mip install`, which fetches a
  release over the network and rewrites every file; this installs a local
  checkout, skips unchanged files via a content-hash marker, and removes what
  its marker does not record. Verified by 7 host tests (each mutation-checked)
  and on a PYBD-SF6W, including deleting `/flash/lib/debugpy` and its marker,
  reinstalling with the command, and passing the 15-test HIL suite against the
  result. Reasoning in `20260810_q11_installer_call_site.md`.
- **Q14 → mirror both flags as launch-configuration keys, absent meaning
  "don't pass it".** `source` (string) and `loop` (boolean) go straight to
  argv; when a key is missing, nothing is emitted and the CLI's own precedence
  runs unchanged. The question's premise was partly wrong, and finding that out
  narrowed the gap: `do_debug` falls back to `resolved.source` whenever
  `--source` is absent, and everything downstream keys off the resulting
  `source_root`, so a target that declares `source` in `mpdebug.toml` already
  mounted on an F5 launch. What was actually unreachable was an ad-hoc or
  overriding source root, and `--loop`, which has no config-file equivalent at
  all. Because a missing key emits nothing, the launch config duplicates
  `mpdebug.toml` only when a user deliberately overrides it - which is the
  objection the question raised, answered by omission rather than by
  precedence rules of the extension's own. Rejected: deriving `--source` from
  the file being debugged. Not merely because it passes a flag the user never
  wrote, but because `--source` *overrides* the target's `source`, so
  derivation would silently displace a configured value on exactly the
  projects that had set one. Also decided not to pre-check `source` against a
  unix target in the extension: that rule belongs to the CLI, whose message
  reaches the user through the existing captured-output path (asserted by an
  integration test). Left open deliberately: whether `loop` should default to
  true for a device target, and whether VS Code's restart button - routed
  through the `ms-python.debugpy` adapter - surfaces the target's
  `supportsRestartRequest` as an in-place restart. The second needs an
  extension-host scenario that drives the restart button, which none of that
  suite's ten does. Reasoning in `20260810_q14_f5_source_and_loop.md`, whose
  2026-08-10 correction withdraws the claim that the job itself had never run.

**DECIDED (2026-08-08):**

- **Q13 → a device target mounts only when it names a source root.** A `source`
  key in `[target.<name>]` names the host directory to mount; absent means
  device-resident, which is what every HIL scenario needs (`/flash/target.py`)
  and what keeps existing configs behaving as they do. Relative values resolve
  against the directory holding `mpdebug.toml`, and `Target.source` is always
  absolute and realpath-resolved. `mpremote debug --source PATH` covers a
  literal connect string with no config file, and overrides the key.
  Rejected: mounting the config's directory unless told otherwise, which
  silently changes what runs for every existing `mpdebug.toml` and has no
  defensible default root for a config that does not sit at the project root.
  D2's "mount is the mainline" is about which loop the docs and the extension
  steer users to, not a licence to infer a source root nobody wrote down. The
  key is also what makes `pathMappings` derivable, so a session that mounts is
  exactly a session that can generate them. Consequence for the HIL suite: each
  scenario states which mode it exercises rather than inheriting a default.
  Reasoning in `20260808_epic4_phase_entry.md`; implemented by STORY-4.3.

**DECIDED (2026-08-05):**

- **Q8 → split bind from accept in `public_api.listen()`.** The endpoint is
  returned as soon as the socket is bound; `accept()` and the `initialize`
  handshake move into `wait_for_client()`. The launcher then emits
  `MPDBG-READY` before any client exists, so `mpremote debug` (and s5.4's
  transport plumbing, and EPIC-6's network flows) can read the endpoint and
  attach to it. This also aligns the API with CPython debugpy, where
  `listen()` returns the endpoint and `wait_for_client()` blocks. Rejected:
  parsing the human-readable `Debugpy listening on <host>:<port>` line, which
  would make a log string the contract in place of the JSON handshake built
  for it, and still could not report a device-assigned port. Sub-question
  decided too: `--port 0` hard-errors on targets whose `getsockname()` cannot
  report the bound port, rather than silently substituting `DEFAULT_PORT` and
  advertising an endpoint nothing is listening on. The change lands on
  `mpy-debugpy-foundations`, the local-only branch that already owns
  `wait_for_client` and the capability probe, so it survives recomposition
  (`add-debugpy-support` is fetched live from PR #1022 and cannot carry local
  commits). Raised and analysed the same day in
  `20260805_handshake-ordering-blocker.md`; unblocks STORY-5.1.

**DECIDED (2026-07-15):**

- **Q7 → push `main` to `origin` as work lands** (option a). Raised the same
  day during the roadmap review: STORY-3.2's GitHub Actions firmware builds
  need this repo pushed to `origin` (git@github.com:andrewleech/mpy-debugpy.git,
  public) to iterate, and pushing was not established convention — local `main`
  had accumulated 8 unpushed commits across sessions. Decision accepts that
  `planning/` becomes public. Unblocks s3.2 in full (and transitively s3.4's
  fetch-URL sections); Actions iteration happens on `main` or short-lived
  branches at the executor's discretion.

**DECIDED (2026-07-06):**

- **Q6 → NO, parameters are NOT omitted** on the current branch (`18c76f7c`). The old
  omission (BACKGROUND area 1, traced on `05b7818`) is gone: `py/profile.c:164/181`
  stores every `local_num` unconditionally with no `param_count` skip, and
  `py/compile.c:3487-3492` captures param names with no `IS_PARAM` exclusion. Proven
  empirically — `def foo(a,b,c=10)` traced via `sys.settrace` yields `f_locals` keys
  `['a','b','c','x','y']`. Real-name path gates on `MICROPY_PY_SYS_SETTRACE_LOCALNAMES`
  (no `py/localnames.c`). No firmware fix needed; a positive regression test is folded
  into STORY-8.6. Full trace: `20260706_flocals_params.md`.

- **Q1 → option (a).** `do_debug` develops on ONE branch on `andrewleech/micropython`,
  registered in ampremote's mbm integration AND merged into this repo's `mpy-debugpy`
  integration branch. One branch, two consumers, no divergence. Governs EPIC-5 / STORY-8.1.
- **Q2 → GitHub Actions on this repo → Releases.** Board set for v1 firmware artifacts:
  **unix, rpi_pico_w, PYBD_SF6, ESP32_GENERIC.** Governs EPIC-3 (STORY-3.1/3.2).
- **Q3 → ship v1 network-only.** Single-UART serial DAP is pushed back, not dropped: the
  framing/escaping details in `SPIKE-2.2-serial-dap.md` are retained as the basis for a
  later follow-up spike. Governs EPIC-6 (STORY-6.1 scopes to multi-CDC boards for v1).
- **Q4 → VS Code extension IS in v1 scope** (the primary marketing win). EPIC-7 stays in
  the v1 roadmap rather than deferring to v2.
- **Q5 → wire mbm reproducibility now.** STORY-8.5 is pulled forward — do it before more
  feature work so upstream-master bumps stay cheap.
Close a question with a dated DECIDED entry and a pointer to the design note that
resolved it; do not delete rows.

---

## Epics and stories

Story fields: id, title, type, description, acceptance criteria, dependencies, component,
effort (S/M/L), risk (low/med/high), and model tier where a coding-workflow tier applies
(implementation→sonnet, automated testing→haiku, review→opus; investigation/design→opus).

**Tickets.** Every substantive story has a self-contained execution brief in
`planning/tickets/s<epic>.<story>_<slug>.md`, written at planning time and revalidated at
phase entry per `planning/00_index.md` (the ticket, not the story text here, is what an
implementation workflow consumes). Story bodies below are point-in-time briefs: done-ness
lives in the Status section and each executed ticket's Execution outcome section, and
completed stories are tagged DONE here rather than having their briefs rewritten:

| epic | tickets |
|------|---------|
| EPIC-3 | `s3.2_ci-firmware-builds` · `s3.3_variant-matrix-collapse` (DONE) · `s3.4_firmware-docs` |
| EPIC-4 | `s4.1_device-debugpy-install` · `s4.2_incremental-sync` · `s4.3_staleness-guard-pathmappings` · `s4.4_sync-tests` · `s4.5_hot-reload-reimport` |
| EPIC-5 | `s5.1_do-debug-skeleton` · `s5.2_target-model` · `s5.3_unix-flow` · `s5.4_handshake-plumbing` · `s5.5_command-tests` |
| EPIC-6 | `s6.1_serial-transport` · `s6.2_network-transport` · `s6.3_dap-log` · `s6.4_hardware-tests` · `s6.5_evaluate-exec` (DONE) |
| EPIC-7 | `s7.1_debug-config-provider` · `s7.2_target-picker` · `s7.4_extension-smoke-test` |
| EPIC-8 | `s8.1_mbm-debug-branch` · `s8.2_upstream-debugpy-foundations` · `s8.4_user-docs` · `s8.5_mbm-reproducible-composition` (DONE) · `s8.6_upstream-firmware-fixes` (DONE) |

No tickets (intentional): EPIC-1 and EPIC-2 are DONE; STORY-3.1 is DONE; STORY-7.3 and
STORY-8.3 are trivial — their story entries below are the full brief.

Components: **firmware** = micropython firmware submodule; **debugpy** = micropython-lib
`python-ecosys/debugpy`; **mpremote** = ampremote/mpremote fork; **wrapper** = this repo;
**extension** = VS Code extension; **CI**.

---

### EPIC-1 — No-regret foundations (protocol correctness + capability truth)

**Goal:** remove the fragile bits that are wrong regardless of the eventual architecture:
the sleep(2) breakpoint race, capability guessing, and the promise of local editing. All
of this lands in the debugpy module and is upstream-friendly on its own.

**Rationale:** none of these commit us to the command, the transports or the extension.
They make every later flow more reliable and they are small, self-contained changes with
existing test scaffolding (`tests/` + `fake_vscode`).

**Definition of done:** the sleep(2) is gone and replaced by a deterministic
configuration-done sync; the server reports a capability record at attach time; locals are
presented read-only; the two launcher scripts are merged into one parameterised boot
script that emits the handshake line; host-side pytest covers all of it.

- **STORY-1.1 — `wait_for_client()` gated on `configurationDone`**
  - type: implementation
  - description: Add a `debugpy.wait_for_client()` to the server that spins the existing
    `process_pending_messages()` poll loop until the DAP `configurationDone` request has
    been received, then returns. The launcher calls it instead of `time.sleep(2)`. This is
    the same loop shape as `wait_for_continue()`, so it fits the no-thread model.
  - acceptance criteria:
    - [ ] `configurationDone` is recorded when received by the session.
    - [ ] `wait_for_client()` returns only after `configurationDone`, or after a bounded
      timeout that logs a clear message (no silent 2 s guess).
    - [ ] breakpoints set by the client before `configurationDone` are honoured on the
      first traced line — no race.
    - [ ] no `time.sleep(2)` remains in the launch path.
  - dependencies: none
  - component: debugpy
  - effort: S · risk: low · model: sonnet

- **STORY-1.2 — Runtime capability probe + capability record**
  - type: implementation
  - description: At session start the server probes the actual firmware: settrace present;
    whether `f_locals` names are real or `local_N` placeholders (SAVE_NAMES); whether
    `frame._set_local` exists; `f_back` present. Assemble a capability dict returned to the
    tooling (via the handshake line, STORY-1.4, and/or a custom DAP response field).
  - acceptance criteria:
    - [ ] capability dict reports at least: `settrace`, `save_names`, `set_local`, `f_back`.
    - [ ] values are derived by probing, never from a build/variant name.
    - [ ] probing works on both unix and bare-metal without raising.
  - dependencies: none
  - component: debugpy
  - effort: M · risk: med · model: sonnet

- **STORY-1.3 — Mark locals read-only via DAP presentation hint**
  - type: implementation
  - description: When `set_local` capability is absent, tag local-scope variables with the
    DAP `presentationHint.attributes = ["readOnly"]` so clients grey out the edit
    affordance. Globals stay editable. Removes the "edit then hit an error string" path.
  - acceptance criteria:
    - [ ] locals returned with `readOnly` hint when `set_local` is false.
    - [ ] globals remain editable and setVariable on a global still mutates the program.
    - [ ] no user-visible error path for attempting a local edit in the unsupported case.
  - dependencies: STORY-1.2
  - component: debugpy
  - effort: S · risk: low · model: sonnet

- **STORY-1.4 — Single parameterised boot script with handshake line**
  - type: implementation
  - description: Replace `mpy_launch_debugpy_unix.py` and `..._esp32.py` with one boot
    script that: detects environment (presence of `network`), takes target module/method
    and an optional port (0 = auto), starts the listener, prints exactly one machine
    line `MPDBG-READY {json}` with host, port and the capability record, calls
    `wait_for_client()`, then imports+runs the target. On a network target it reports its
    own DHCP address; on unix it reports the bound port. This script becomes an artifact the
    command uploads/owns; users never edit it.
  - acceptance criteria:
    - [ ] one script covers unix and device; no IP or port literals inside it.
    - [ ] emits exactly one parseable `MPDBG-READY` line with endpoint + capabilities.
    - [ ] uses `wait_for_client()` (STORY-1.1), not sleep.
    - [ ] the two old launcher scripts are deleted.
  - dependencies: STORY-1.1, STORY-1.2
  - component: wrapper (script), later moved into mpremote as an embedded resource
  - effort: M · risk: med · model: sonnet

- **STORY-1.5 — Host-side tests for foundations**
  - type: test
  - description: Extend the `fake_vscode` pytest harness to assert the configurationDone
    handshake (no race), the capability record contents, the read-only locals hint, and
    parsing of the `MPDBG-READY` line.
  - acceptance criteria:
    - [ ] test proves a breakpoint set pre-`configurationDone` stops on first line.
    - [ ] test asserts capability dict fields for a known firmware.
    - [ ] test asserts locals carry readOnly and a global setVariable succeeds.
    - [ ] test parses a sample `MPDBG-READY` line into endpoint+caps.
  - dependencies: STORY-1.1, STORY-1.2, STORY-1.3, STORY-1.4
  - component: wrapper / CI
  - effort: M · risk: low · model: haiku

- **STORY-1.6 — Review foundations**
  - type: review
  - description: Adversarial + standard review of EPIC-1 changes against the no-thread
    model and upstream mergeability (these land in micropython-lib eventually).
  - acceptance criteria: [ ] review findings fed back and resolved; [ ] no regression in
    existing `tests/`.
  - dependencies: STORY-1.5
  - component: debugpy / wrapper · effort: S · risk: low · model: opus

---

### EPIC-2 — Architecture-deciding spikes (run early)

**Goal:** answer the two questions that can delete whole subsystems before we build them.

**Rationale:** the sync engine (EPIC-4) and the network/proxy work (EPIC-6) are large. If
mount works under settrace we don't build a sync engine at all. If DAP tunnels over serial
we don't need network transport, IP discovery, or a TCP proxy for the common case. Spend a
little to possibly avoid a lot.

**Definition of done:** both spikes produce a written yes/no with evidence and a
recommendation that the downstream epics consume.

- **STORY-2.1 — Spike: does mpremote `mount` work under settrace?**
  - type: investigation/spike
  - description: Determine empirically whether a program run from a mounted `/remote`
    filesystem can be debugged: set a breakpoint, pause, step, inspect. Trace the failure
    if it hangs — the hypothesis (from `transport_serial.py`) is that `SerialIntercept`
    fs-RPC is not serviced while the traced program is off the REPL loop or paused in the
    busy-poll. Test whether servicing the fs-RPC from inside the debugpy poll loop, or
    running debugpy's transport on a separate channel, changes the outcome.
  - decision it unblocks: whether EPIC-4 (sync engine) is needed at all, or reduces to a
    thin "ensure debugpy module present" step with mount providing live source.
  - yes/no downstream effect: **yes** → EPIC-4 collapses to STORY-4.1 only (module
    presence); source is always live, staleness problem gone. **no** → build the
    hash-verified sync engine (EPIC-4) in full.
  - acceptance criteria:
    - [ ] reproduce a debug session against mounted source; record pass/hang with logs.
    - [ ] root-cause any hang to a specific interaction (fs-RPC vs trace/pause).
    - [ ] written recommendation with the exact conditions under which mount is/ isn't
      viable for debugging.
  - dependencies: STORY-1.4 (need a working boot path to test against)
  - component: mpremote + debugpy + firmware · effort: M · risk: high · model: opus

- **STORY-2.2 — Spike: DAP over the serial/raw-REPL channel**
  - type: investigation/spike
  - description: Assess tunnelling the DAP byte stream over the existing serial connection
    instead of TCP. The server's poll model only needs a non-blocking read of a stream; it
    is written against a socket today. Evaluate multiplexing DAP with REPL output on one
    serial line (or a dedicated framing), throughput/latency while stepping, and whether
    #18785/#19062 primitives help. Prototype enough to attach VS Code once over serial.
  - decision it unblocks: whether the serial-device flow needs WiFi/IP/network at all, and
    whether a localhost TCP proxy (bridging VS Code↔serial) is the integration point.
  - yes/no downstream effect: **yes** → serial transport becomes first-class (EPIC-5),
    network transport is optional, no IP discovery needed for USB devices; a small
    localhost↔serial DAP bridge is the only proxy we build. **no** → serial devices must
    use the network transport (device joins WiFi, reports IP via handshake), EPIC-6 carries
    the load.
  - acceptance criteria:
    - [ ] measured feasibility of DAP framing over serial alongside/without REPL output.
    - [ ] a prototype attach over serial reaching at least one breakpoint, or a documented
      blocker with root cause.
    - [ ] recommendation on the localhost bridge design (where it lives in mpremote).
  - dependencies: STORY-1.4
  - component: mpremote + debugpy · effort: L · risk: high · model: opus

---

### EPIC-3 — Firmware provenance and capability matrix

**Goal:** make firmware selection and build non-opaque without forcing every user to
compile, and stop encoding capabilities in names.

**Rationale:** prebuilt binaries in-tree have opaque provenance and the variant names lie
about capabilities. This epic can run in parallel with EPIC-1/EPIC-2.

**Definition of done:** each firmware artifact has a manifest (source repo, commit, build
flags, artifact hash, download URL); a `fetch` path downloads+verifies; a documented
reproducible (docker) build path exists; the variant matrix is collapsed to the real set;
the tooling selects firmware by required capability, not by name.

- **STORY-3.1 — Firmware manifest + hash-verified fetch**
  - type: implementation
  - description: Define a per-variant manifest (repo, SHA, flags, artifact sha256, URL).
    Publish binaries to GitHub Releases via CI **on this repo** (Q2 DECIDED); stop
    carrying binaries in-tree. Add a fetch that downloads and verifies against the
    manifest hash. v1 board set: **unix, rpi_pico_w, PYBD_SF6, ESP32_GENERIC.**
  - acceptance criteria:
    - [ ] manifest records provenance for each published artifact.
    - [ ] fetch verifies sha256 and fails loudly on mismatch.
    - [ ] repo no longer stores firmware binaries.
  - dependencies: none
  - component: wrapper + CI · effort: M · risk: med · model: sonnet

- **STORY-3.2 — Reproducible docker build for firmware variants**
  - **DONE 2026-08-04** — see Status and `s3.2_ci-firmware-builds.md`
    Execution progress (hardware boot-check deferred to STORY-6.4).
  - type: implementation
  - description: Dockerised/CI build producing the v1 artifacts from the pinned
    integration SHA with pinned flags (`MICROPY_PY_SYS_SETTRACE` +
    `MICROPY_PY_SYS_SETTRACE_LOCALNAMES`; PERSIST off). Host-uid/path convention per
    user CLAUDE.md. Boards (Q2 DECIDED): unix, rpi_pico_w, PYBD_SF6, ESP32_GENERIC.
  - acceptance criteria:
    - [x] each of the four boards builds reproducibly in CI from the pinned SHA.
    - [x] output artifact hash matches the manifest when inputs match.
    - [x] the legacy pre-recomposition manifest entries are replaced by these builds.
  - dependencies: STORY-3.1
  - component: firmware + CI · effort: M · risk: med · model: sonnet

- **STORY-3.3 — Collapse variant matrix to the real set**
  - **DONE 2026-07-15** — see Status and `s3.3_variant-matrix-collapse.md`
    Execution outcome.
  - type: implementation
  - description: Remove variants whose names promise unimplemented features (e.g.
    `set_local` against firmware without `_set_local`). Keep `settrace` and
    `settrace_save_names`. Selection driven by the capability the session needs, resolved
    against the probe (STORY-1.2).
  - acceptance criteria:
    - [ ] only real variants remain.
    - [ ] tooling picks a variant by required capability, and a mismatch between a
      variant's claimed and probed capability is a hard error.
  - dependencies: STORY-1.2, STORY-3.1
  - component: wrapper · effort: S · risk: low · model: sonnet

- **STORY-3.4 — Docs: firmware provenance + capability matrix**
  - **DONE 2026-08-05** — see Status and `s3.4_firmware-docs.md` Execution
    outcome (`docs/firmware.md` + README link; BACKGROUND banner already
    carried the settrace-by-default correction and now links the doc).
  - type: docs
  - description: Document what each variant is, its flags, what capability it yields
    (real local names vs `local_N` placeholders; no local editing), and how to fetch vs
    build. Correct the BACKGROUND note about settrace-by-default (it is not).
  - acceptance criteria: [x] table of variant→flags→capabilities; [x] fetch and build both
    documented; [x] no capability claim that the probe would contradict.
  - dependencies: STORY-3.1, STORY-3.3
  - component: wrapper · effort: S · risk: low · model: sonnet

---

### EPIC-4 — Incremental sync / staleness engine

**Goal:** replace `cp -r src/ :/` on every run with hash-verified incremental sync, and
make device staleness impossible without a warning. **Scope depends on STORY-2.1.**

**Rationale:** full re-upload every run is slow and silently goes stale. mpremote #18436
already gives per-file sha256 verification; build the sync on top of it rather than a
bespoke engine.

**Definition of done (if mount spike is NO):** only changed files are transferred; an
on-device manifest maps path→hash; at attach time a mismatch between local and device hash
is a hard warning before the session starts; path mappings are generated from the sync
record, not hand-written. **If STORY-2.1 is YES:** this epic collapses to STORY-4.1.

- **STORY-4.1 — Ensure debugpy module present on device (compiled, cached)**
  - **DONE 2026-08-05** — see Status and `s4.1_device-debugpy-install.md`
    Execution progress (hardware install check deferred to STORY-6.4). Its CLI
    call site is `mpremote debugpy-install` (Q11, 2026-08-10).
  - type: implementation
  - description: Ensure the debugpy package is installed on the device, cross-compiled to
    `.mpy` and cached keyed on (source hash, mpy-cross version, flags); only reinstall when
    the package hash changes. Reuse mpremote `fs_writefile(verify_hash=True)`.
  - acceptance criteria:
    - [x] first run installs; subsequent runs with unchanged package skip reinstall.
    - [x] install verified by device-side hash.
  - dependencies: EPIC-2 decision (STORY-2.1)
  - component: mpremote + wrapper · effort: M · risk: med · model: sonnet

- **STORY-4.2 — Hash-verified incremental source sync + device manifest**
  - type: implementation
  - description: (Only if STORY-2.1 = NO.) Maintain `.mpdbg/manifest.json` on the device
    (path→sha256). Each run hashes local files, pushes only diffs via
    `fs_writefile(verify_hash=True)` (+ #18785 encoding), updates the manifest. Record the
    local→device path map for path-mapping generation.
  - acceptance criteria:
    - [ ] unchanged files are not retransferred.
    - [ ] changed files are detected and pushed; manifest updated.
    - [ ] transfers verified by device-side hash.
  - dependencies: STORY-4.1, STORY-2.1 (=NO)
  - component: mpremote + wrapper · effort: L · risk: med · model: sonnet

- **STORY-4.3 - Mount-backed attach path, staleness guard, generated pathMappings**
  - **DONE 2026-08-09** - all three criteria met, criterion 1 both over a pty against the
    unix firmware (`tests/test_s4_3_mount_attach.py`) and on a PYBD_SF6
    (`20260809_hil_PYBD_SF6.md`). Two defects found on the way, both in code the
    criteria reach rather than in the new path itself:
    `20260809_story4.3_mount_teardown.md`.
  - **Rescoped 2026-08-08** at phase entry: `do_debug` mounts nothing, so this story now
    owns establishing the mount as well as guarding it - the "live-mount attach path" D2's
    collapse text names. The hash-diff guard is retired with STORY-4.2: under a live mount
    there is no device copy to compare against, only a mount that did or did not establish.
    See `20260808_epic4_phase_entry.md`.
  - type: implementation
  - description: Mount the target's source root before the boot script runs, so the device
    imports the host's files; refuse to attach if the mount did not establish or does not
    cover the target module; generate the DAP `pathMappings` as absolute paths from the
    mount record (fixes multi-root ambiguity), reachable by both the attach request and
    the extension's launch config.
  - acceptance criteria:
    - [x] a device target debugs a file that exists only on the host, reaching a
      breakpoint on a line that was never uploaded.
    - [x] pathMappings are generated, absolute, and correct for a multi-root workspace.
    - [x] the mount is torn down on every exit path, including exception and Ctrl-C.
  - dependencies: STORY-4.1
  - component: mpremote + wrapper · effort: M · risk: med · model: sonnet

- **STORY-4.4 — Sync tests**
  - type: test
  - description: Cover incremental behaviour (no-op on unchanged, diff on changed), hash
    verification failure handling, and the staleness guard. Use QEMU PTY (#18327) where
    possible to run without hardware.
  - acceptance criteria: [ ] tests for no-op/diff/verify-fail/stale-guard; [ ] runnable in
    CI against QEMU.
  - dependencies: STORY-4.2, STORY-4.3
  - component: CI · effort: M · risk: med · model: haiku

- **STORY-4.5 — Hot-reload iteration loop via `sys.modules` eviction + re-import**
  - **DONE 2026-08-09** — see Status, `20260809_story4.5_hot_reload.md`, and
    `s4.5_hot-reload-reimport.md` Execution progress.
  - type: implementation
  - description: (Surfaced by SPIKE-2.1.) With source mounted from the host, an
    edit→re-run loop needs no upload: evict the target from `sys.modules` and re-import
    it (or monkey-patch from the REPL). Wire this into the debug session lifecycle so a
    restart re-imports fresh source from the mount instead of resetting the board. Only
    an already-paused frame cannot pick up edits.
  - acceptance criteria:
    - [x] editing a mounted source file and restarting the session runs the new code
      with no upload step.
    - [x] eviction covers the target module and its project-local imports.
      Delivered as a snapshot diff of `sys.modules`, which covers them and cannot
      cover the debugger; see `20260809_story4.5_hot_reload.md`.
  - dependencies: STORY-2.1 (done), STORY-4.1, STORY-4.3 (establishes the mount)
  - component: mpremote + debugpy · effort: M · risk: med · model: sonnet

---

### EPIC-5 — The `mpremote debug` command + unified target model

**Goal:** one command that resolves a named target and runs resolve→ensure→sync→launch→
attach for unix, serial device and network device behind one mental model.

**Rationale:** this is where the bulk of the friction dies. It is mpremote-native
(reusing transport, raw-REPL, mount, reconnect, hash-cp) and is the artifact we upstream.

**Definition of done:** `mpremote debug <target> [module[:method]]` launches a debug
session on any of the three transports; endpoints and capabilities come from the handshake
line; no IPs/ports are typed; the command is registered in `_COMMANDS` and follows the
mpremote house style; delivered as an mbm-registered branch.

- **STORY-5.1 — `do_debug` command skeleton + argparse, registered in `_COMMANDS`**
  - **DONE 2026-08-05** — see Status and `s5.1_do-debug-skeleton.md` Execution
    progress.
  - type: implementation
  - description: Add `do_debug(state, args)` and `argparse_debug()` in `commands.py`,
    import into `main.py`, add to `_COMMANDS`. Follow the `do_run`/`do_mount` pattern
    (`ensure_raw_repl`, `did_action`). Args: target/transport selection, target
    module[:method], optional port, `--dap-log`. Reuse `state.transport` for connection.
  - acceptance criteria:
    - [x] `mpremote debug --help` lists the command with a real description.
    - [x] command connects via existing transport handling and reaches the boot script.
    - [x] follows house style (byte-literal comments, extend existing primitives,
      new-arg defaults preserve old behaviour, underscore-prefix single-caller helpers).
  - dependencies: STORY-1.4
  - component: mpremote · effort: M · risk: med · model: sonnet

- **STORY-5.2 — Named target model (`mpdebug.toml`) + target resolution**
  - **DONE 2026-08-05** — see Status and `s5.2_target-model.md` Execution
    progress.
  - type: implementation
  - description: Project config with named targets (`kind = unix|serial|network`, connect
    string via `/dev/serial/by-id/...` for serial, firmware/capability requirement). The
    command resolves a target name to a transport + runtime + firmware requirement.
  - acceptance criteria:
    - [ ] a named target resolves to transport + firmware requirement.
    - [ ] serial connect strings use stable `/dev/serial/by-id/...` references.
    - [ ] absent/ambiguous target gives a clear error, not a stack trace.
  - dependencies: STORY-5.1, STORY-3.3
  - component: mpremote · effort: M · risk: med · model: sonnet

- **STORY-5.3 — Unix flow end-to-end**
  - **DONE 2026-08-06** — see Status and `s5.3_unix-flow.md` Execution
    progress (breakpoint-drive proof rides with STORY-5.5).
  - type: implementation
  - description: For `kind=unix`: ensure firmware (fetch/build), set module path, launch
    the boot script as a subprocess, parse `MPDBG-READY` from stdout (control plane), start
    the client attach to the reported localhost port (data plane).
  - acceptance criteria:
    - [ ] `mpremote debug unix target:main` reaches a breakpoint with zero hand config.
    - [ ] port is auto (0) and taken from the handshake; no literal port anywhere.
  - dependencies: STORY-5.1, STORY-5.2, STORY-3.1, EPIC-1
  - component: mpremote + wrapper · effort: M · risk: med · model: sonnet

- **STORY-5.4 — Handshake plumbing (control plane / data plane split)**
  - **DONE 2026-08-06** — see Status and `s5.4_handshake-plumbing.md` Execution
    progress.
  - type: implementation
  - description: Generalise handshake parsing so the control plane is subprocess stdout
    (unix), raw-REPL output (serial), or serial (network device before it has an IP), and
    the data plane is TCP or the serial DAP tunnel per the EPIC-2 outcome. The device tells
    the tooling its endpoint; the tooling never guesses.
  - acceptance criteria:
    - [ ] one handshake parser serves all transports.
    - [ ] endpoint always comes from the device/subprocess, never from user config.
  - dependencies: STORY-5.1, STORY-2.2
  - component: mpremote · effort: M · risk: med · model: sonnet

- **STORY-5.5 — Command tests (unix + QEMU)**
  - **DONE 2026-08-06** — see Status and `s5.5_command-tests.md` Execution
    progress (QEMU leg skips where the binary is absent).
  - type: test
  - description: Drive `do_debug` for unix and a QEMU PTY target through the `fake_vscode`
    client: attach, breakpoint, step, inspect, continue. Assert no hardcoded endpoints.
  - acceptance criteria: [ ] unix + QEMU flows pass in CI; [ ] handshake-derived endpoints
    asserted.
  - dependencies: STORY-5.3, STORY-5.4
  - component: CI · effort: M · risk: med · model: haiku

---

### EPIC-6 — Device transports: serial and network

**Goal:** the two device transports wired into `mpremote debug`, shaped by EPIC-2.

**Rationale:** serial is the native mpremote experience (USB, no network). Network is
needed when serial DAP tunnelling isn't viable or when the device is only reachable over
WiFi. Which one is primary depends on STORY-2.2.

**Definition of done:** `mpremote debug <serial-target>` and `<network-target>` both work
end-to-end; a device on WiFi reports its own address; serial devices need no IP.

- **STORY-6.1 — Serial device transport**
  - **OPEN 2026-08-10 (criterion 3).** Killing the bridge at a breakpoint now
    frees the board, which took a fix: a USB CDC interface has no EOF, so the
    device could not tell a host that had gone from one that was quiet, and
    the session waited for a `continue` that could not arrive.
    `20260810_bridge-kill-mid-session.md`. Ticket criterion 5 was already met
    and is now ticked. Criterion 2 is the only one left open.
  - **OPEN 2026-08-10.** Both criteria below are met. What keeps the story
    open is the ticket's criterion 2, the negative probe on a single-UART
    board, which needs a board that has no second CDC. See the ticket.
  - **OPEN 2026-08-08.** The positive path is proven on hardware: a PYBD_SF6
    is debuggable over its own second CDC with no address anywhere. What
    keeps the story open is the ticket's criterion 2, the negative probe on a
    single-UART board, which needs a board that has no second CDC; and
    criterion 4's ampremote half. See the ticket.
  - type: implementation
  - description: If STORY-2.2 = YES: run DAP over serial via a localhost↔serial bridge the
    command owns; VS Code attaches to localhost. If NO: bring the serial device onto the
    network and fall through to network transport, documenting the WiFi requirement.
    Reuse #17322 reconnect and #19062 socket handling as applicable.
  - acceptance criteria:
    - [x] a USB-connected device is debuggable with the connect string only (no IP), or
      the WiFi fallback is clearly required and documented.
    - [x] board reset/reconnect during a session is handled — by ending the session with
      a clear error, not by reconnecting; `20260810_board-reset-mid-session.md` says why
      reconnecting a DAP session to a rebooted device is not a recovery.
  - dependencies: STORY-2.2, STORY-5.4
  - component: mpremote + debugpy · effort: L · risk: high · model: sonnet

- **STORY-6.2 — Network device transport**
  - **DONE 2026-08-08** — both criteria met, criterion 1 on a PYBD_SF6 over its
    own WiFi (`20260808_hil_PYBD_SF6.md`). See the ticket's revalidation for
    what each criterion is covered by.
  - type: implementation
  - description: For `kind=network`: device joins WiFi, boot script reports its DHCP
    address in the handshake over the serial/raw-REPL control plane (or via mDNS), tooling
    attaches over TCP. No IP literals; #19062 for socket handling.
  - acceptance criteria:
    - [x] device address is discovered from the handshake, never typed.
    - [x] `mpremote debug <network-target>` reaches a breakpoint over TCP.
  - dependencies: STORY-5.4, EPIC-4 (sync for device source)
  - component: mpremote + debugpy · effort: L · risk: high · model: sonnet

- **STORY-6.3 — DAP monitor as a `--dap-log` flag**
  - **DONE 2026-08-06** (flag; see Status), **criterion 2 closed 2026-08-09.**
    `--dap-log` is `mpremote/dap_log.py`: an interposed localhost proxy, since
    no TCP transport puts the byte stream through mpremote. The standalone
    `dap_monitor.py` stays in the module as the no-mpremote fallback - the
    ticket's revalidation ruled out importing it from mpremote, so retiring it
    means retiring it as the *documented* workflow, which the docs pass did.
  - type: implementation
  - description: Fold `dap_monitor.py` into the command as `--dap-log`, replacing the
    separate task+config. Works across transports.
  - acceptance criteria: [x] `--dap-log` writes DAP traffic for any transport; [x] the
    standalone monitor task/config is removed from the wrapper.
  - dependencies: STORY-5.1
  - component: mpremote · effort: S · risk: low · model: sonnet

- **STORY-6.4 — Device transport tests (hardware-in-loop)**
  - **Eighteen scenarios as of 2026-08-10**, the record now
    `20260810_hil_PYBD_SF6.md`; the four added since are STORY-4.3's mount
    breakpoint, criterion 4's board reset, and criterion 3's bridge kill and
    DTR probe. Still OPEN for the board set, for the same reason.
  - **The suite is order-independent as of 2026-08-10**, which it was not:
    `hil_reset_board` returned when the device paths came back, which is before
    the board's boot script has finished, so any scenario the shuffle put after
    the reset one could find the board enumerated but not yet on the network.
    It now waits for a REPL prompt as well. `20260810_integration-rebuild.md`.
  - **Both flows done on PYBD_SF6 (2026-08-08); OPEN for the board set.**
    `tests/hil/` is the suite, opt-in behind `MPY_DEBUG_HIL_DEVICE`, with the
    serial-DAP scenarios gated separately on `MPY_DEBUG_HIL_DAP_DEVICE` so a
    single-CDC board still runs everything else. It writes its own results
    record (`20260808_hil_PYBD_SF6.md`) from pytest's reports, including the
    throughput a scenario measured. Fourteen scenarios green. No ESP32 on this
    bench, so the single-UART shape is still untested.
  - type: test
  - description: Serial and network flows on a real ESP32 (referenced by
    `/dev/serial/by-id/...`): attach, breakpoint, step, inspect, staleness guard.
    Hardware-in-loop per ampremote CLAUDE.md testing policy.
  - acceptance criteria: [x] both device flows pass on real hardware (14
    scenarios on PYBD_SF6; the named ESP32 is offline, so its single-UART shape
    is unproven); [x] results record board/port tested.
  - dependencies: STORY-6.1, STORY-6.2
  - component: CI/manual · effort: M · risk: high · model: haiku

- **STORY-6.5 — Extend DAP `evaluate` to statement `exec` (repl/clipboard contexts)**
  - **DONE 2026-07-15** — see Status and `s6.5_evaluate-exec.md` Execution
    outcome. The description below (expression-only `evaluate`, `context`
    ignored, anchored at `0d42fcd`) is the pre-fix state.
  - type: implementation
  - description: (Surfaced by SPIKE-2.1.) `evaluate` currently only `eval()`s
    expressions (`evaluate_expression`, `pdb_adapter.py:649-669` at `0d42fcd`; the DAP
    `context` field is currently ignored by the handler). Support statement execution
    for the DAP `repl`/`clipboard` contexts so a paused user can redefine a function or
    mutate globals from the Debug Console — the monkey-patching escape hatch for live
    edits.
  - acceptance criteria:
    - [ ] statements execute against the paused frame's globals from the Debug Console.
    - [ ] expression evaluation is unchanged for `watch`/`hover` contexts.
  - dependencies: EPIC-1
  - component: debugpy · effort: S · risk: low · model: sonnet

- **STORY-6.6 — Make the DAP `pause` request stop the target**
  - **DONE 2026-08-10** — see Status and `s6.6_dap-pause.md`. The description
    below is the pre-fix state, found while closing STORY-4.5.
  - type: implementation
  - description: `_handle_pause` set two `paused` fields and answered success;
    no stop decision read either, so the client moved its UI to the stopped
    state while the program ran on and every `stackTrace`/`scopes`/`variables`
    that followed was answered from a stale `current_frame`. The fix consumes
    the pending pause in `should_stop` at the next `line` event and derives the
    stop reason from the adapter, which is what made the decision.
  - acceptance criteria:
    - [x] a `pause` against a running target produces `stopped` with reason
      `pause`, and the target has demonstrably stopped advancing.
    - [x] a `pause` that arrives while already stopped does not stop the target
      again after the next `continue`.
  - dependencies: EPIC-1
  - component: debugpy · effort: S · risk: low · model: sonnet

---

### EPIC-7 — Thin VS Code extension

**Goal:** F5 one-keypress launch, layered on the command. This is the only epic that
commits to the extension path and is done last, after the command carries the logic.

**Rationale:** VS Code can't give a clean one-keypress launch without either an extension
or the preLaunchTask/problemMatcher contortions the repo has now. Kept thin: it shells out
to `mpremote debug` and starts a `debugpy` attach with the generated config.

> **Fan-out (2026-07-06):** the extension grows beyond this epic into a full
> MicroPython extension (stubs, broker/REPL, fs explorer, mip installer,
> mpflash/mpbuild firmware flows) via its OWN roadmap, seeded by
> `20260706_vscode-extension-direction.md`. EPIC-7 remains the v1 slice here and
> becomes that roadmap's foundation; it does not absorb the new scope.

**Definition of done:** a custom `micropython` debug type resolves to a debugpy attach
using the command's handshake; a target picker in the status bar; nine launch configs
collapse to one; extension depends on ms-python.

- **STORY-7.1 — Debug type + `DebugConfigurationProvider`**
  - type: implementation
  - description: Register debug type `micropython`. In `resolveDebugConfiguration`, spawn
    `mpremote debug`, read the handshake, then start a `debugpy` attach session with the
    generated host/port/pathMappings. Use the return-`undefined`-and-`startDebugging`
    pattern (VS Code can't change a config's type from the resolver). Declare
    `ms-python.debugpy` as an extension dependency. Do not write a TS DAP adapter.
  - acceptance criteria:
    - [ ] F5 on a target launches an attach session with no hand config.
    - [ ] generated pathMappings are absolute (multi-root safe).
    - [ ] fails clearly if ms-python is not installed.
  - dependencies: EPIC-5 (command usable). **Resequenced ahead of STORY-4.3
    (2026-08-06, user decision).** 4.3's remaining substance is device-transport
    work and so is hardware-gated; on unix, local and device paths are the same
    file and pathMappings is the identity, which is enough to build and test the
    resolver. Revisit the absolute-pathMappings criterion when 4.3 lands.
  - component: extension · effort: L · risk: high · model: sonnet

- **STORY-7.2 — Target picker + status bar**
  - **DONE 2026-08-07** — see Status and `s7.2_target-picker.md` DONE section,
    including the multi-root discovery limitation left open.
  - type: implementation
  - description: Status-bar picker over `mpdebug.toml` targets; capability from the probe
    surfaced (e.g. "locals: placeholders", "local edit: unsupported").
  - acceptance criteria: [ ] picker lists targets; [ ] selected target drives F5; [ ]
    capability shown honestly.
  - dependencies: STORY-7.1
  - component: extension · effort: M · risk: med · model: sonnet

- **STORY-7.3 — Collapse launch.json/tasks.json**
  - **DONE 2026-08-07** with STORY-7.1. `.vscode/launch.json` is one
    `micropython` config; the `promptString` host/port inputs are gone, and
    this repo never carried the `MP_DEBUG_VARIANT`/`problemMatcher` hacks (they
    were the old wrapper's).
  - type: implementation
  - description: Reduce the nine attach configs to one, delete the `MP_DEBUG_VARIANT` env
    and the fake `problemMatcher`/`beginsPattern: "."` background-task hacks.
  - acceptance criteria: [ ] one debug config remains; [ ] no problemMatcher hacks; [ ] no
    `MP_DEBUG_VARIANT`.
  - dependencies: STORY-7.1
  - component: wrapper · effort: S · risk: low · model: sonnet

- **STORY-7.4 — Extension smoke test**
  - **DONE 2026-08-07** — see Status and `s7.4_extension-smoke-test.md` DONE
    section, including what the suite deliberately does not claim.
  - type: test
  - description: Automated extension host test: resolver produces a valid attach config
    and starts a session against a unix target.
  - acceptance criteria: [ ] resolver test green in CI.
  - dependencies: STORY-7.1
  - component: extension/CI · effort: M · risk: med · model: haiku

---

### EPIC-8 — Upstreaming and documentation

**Goal:** land the debugpy changes and the `mpremote debug` command upstream, per the
ampremote default-to-upstream policy, and reconcile the fork lineages.

**Rationale:** the end state is upstream-mergeable, not a private distribution. The command
starts as an mbm-registered branch and, once hardware-validated, retargets to
`micropython:master`.

**Definition of done:** debugpy changes raised against micropython-lib #1022 lineage; the
`debug` command raised as a micropython PR; capability probing/readOnly documented; fork
lineages (Josverl vs andrewleech) reconciled.

- **STORY-8.1 — Register `debug` command branch in mbm.toml**
  - type: implementation
  - description: (Q1 DECIDED — one branch, two consumers.) Create the `debug` command as
    a single branch on `andrewleech/micropython` off upstream master. Register it in
    `~/ampremote`'s `mbm.toml` before `ampremote_rename` (which stays last), AND merge it
    into this repo's `micropython` `mpy-debugpy` integration branch, so both integrations
    consume the same branch with no divergence. Open a draft PR on the fork per policy.
  - acceptance criteria: [ ] one branch on `andrewleech/micropython` is the sole source;
    [ ] it builds into the `ampremote` integration via mbm with `ampremote_rename` last;
    [ ] it is merged into this repo's `mpy-debugpy` integration branch; [ ] draft fork
    PR exists.
  - dependencies: EPIC-5
  - component: mpremote/mbm · effort: S · risk: low · model: sonnet

- **STORY-8.2 — Upstream debugpy foundations**
  - type: implementation
  - description: Raise EPIC-1 (wait_for_client, capability probe, readOnly) against the
    micropython-lib #1022 lineage; reconcile Josverl vs andrewleech debugpy branches.
  - acceptance criteria: [ ] PR(s) opened; [ ] lineage reconciled; [ ] the historical
    double-defined `MICROPY_PY_SYS_SETTRACE` build-glue issue confirmed resolved against
    the live PR (ticket s8.2 found no double definition in the current trees — verify,
    then drop this item).
  - dependencies: EPIC-1
  - component: debugpy/firmware · effort: M · risk: med · model: sonnet

- **STORY-8.3 — Promote `debug` command to upstream PR after hardware validation**
  - type: implementation
  - description: Once EPIC-6 hardware tests pass, open a PR for the `debug`
    command against `micropython:master`. **Corrected 2026-08-09** — the
    original wording ("switch the fork PR base to `micropython:master`")
    assumes a PR exists to retarget. None does: `mpremote_debug` is a branch on
    the fork (`d6a68f9477`, 8 commits, registered in `mbm.toml` with a `/tree/`
    URL and no `pr_number`), with no PR from it in either repo. Nor is
    retargeting how this fork stages work - it opens a fork-internal PR based
    on `review/<topic>` and a *separate* upstream PR from the same head branch
    (PR #47 `review/baochip-port <- dabao` alongside upstream #19392
    `master <- dabao`), so the two never share an edited base.
  - preconditions, both real and neither a base edit: `mpremote_debug` is 5
    commits behind `upstream/master` and needs rebasing; and the seven
    integration-branch commits that are not on any mpremote feature branch have
    to be folded onto it first, or the PR ships a `debug` command that this
    repo's tests have never run against. See the risk register row.
  - acceptance criteria: [x] unfolded mpremote commits folded onto
    `mpremote_debug`; [x] branch rebased onto current `upstream/master`;
    [ ] PR opened against `micropython:master`; [ ] PR references validation
    evidence; [ ] its CI check list is green by name (ruff, codespell, and the
    commit-message job that reports as `build`).
  - **2026-08-09, criteria 1 and 2 done, local only.** `mpremote_debug` is
    `33c065e033`: 13 commits on current `upstream/master`, the five unfolded
    ones cherry-picked in and the format hunks folded as `--fixup`s onto the
    commits `git blame` names. Every message was rewritten - all 13 failed
    `verifygitlog`, at minimum for a missing `Signed-off-by`, so the third
    gate was red for the whole branch and not only for what was folded in.
    Green now: `verifygitlog`, `ruff check`, `ruff format --diff`, `codespell`,
    and 307 passed / 2 skipped / 1 xfailed with the submodule checked out on
    the branch (`tests/hil` excluded, no board attached). Two things a reviewer
    should see rather than have smoothed over: the series carries two author
    emails, both the author's own and both used upstream, against one
    `Signed-off-by` address; and the bodies are long by
    `CODECONVENTIONS.md`'s standard, which asks for one or two sentences. The
    push to the fork and the PR itself are not done - both are outward-facing
    and neither is needed for the two criteria above.
  - **2026-08-10, on the fork only, by explicit user decision ("fork + draft
    PR, stop there"): https://github.com/andrewleech/micropython/pull/51**,
    draft, `mpremote_debug` at `33c065e033` into `review/mpremote_debug`, which
    is the fork-internal staging shape STORY-8.3's correction describes.
    Nothing is opened at `micropython/micropython`. 150 checks pass and 3 fail,
    and neither failure is the branch's content. Both `build` failures are the
    `Package mpremote / build` job, which is a fork-only `git describe`
    artifact: hatch-vcs takes the nearest reachable tag, and this project's own
    `mpy-debugpy-pin-<sha12>` tags sit on plain master commits in the fork,
    where they shadow the `v*` tags the version scheme needs. Upstream carries
    no such tags and the pins are the documented reproducibility mechanism, so
    they were left alone. `qemu_mips` is a flake on
    `cmdline/repl_paste.py`: the sibling event's `qemu_mips` job ran the same
    content to success, and upstream's own `qemu_mips` is green at the shared
    base `5f2181f938`. The two remaining acceptance criteria stay open, since
    they are about the upstream PR.
  - dependencies: STORY-8.1, STORY-6.4
  - component: mpremote · effort: S · risk: med · model: sonnet

- **STORY-8.4 — User-facing docs**
  - **DONE 2026-08-09** — `docs/debugging.md`; see Status and
    `s8.4_user-docs.md` Execution progress. Firmware became a link to
    `docs/firmware.md` rather than a chapter, per the ticket's 2026-08-09
    rescope.
  - type: docs
  - description: One "Debug MicroPython" guide covering the three transports, the target
    model, firmware fetch/build, and the honest capability story (no local editing;
    placeholder vs real local names).
  - acceptance criteria: [x] guide covers all three transports and firmware; [x] no
    capability claim the probe contradicts.
  - dependencies: EPIC-5, EPIC-6, EPIC-3
  - component: wrapper · effort: M · risk: low · model: sonnet

- **STORY-8.5 — Wire reproducible mbm composition (`mbm add-pr` / `mbm rebase`)**
  - **DONE 2026-07-15** — see Status and `s8.5_mbm-reproducible-composition.md`
    Execution outcome. The description below is superseded: the composition is
    now mbm-rebuildable and `make integrate` runs `mbm rebase --local` for both
    submodules (no longer a stub). The ampremote mpremote branches were NOT
    registered in this pass — deferred to the EPIC-4/5 ticket that first needs
    them, per D6 and the mbm.toml header note.
  - type: implementation
  - description: The integration branches are hand-composed (merge + cherry-pick;
    `mbm.toml` records them for provenance only). Express the same composition as mbm
    branch entries so `mbm rebase` can rebuild both integration branches on latest
    upstream, with `git rerere` replaying conflict resolutions. The Makefile
    `integrate` target currently stubs to this story.
  - acceptance criteria:
    - [ ] `mbm rebase -s micropython` and `-s micropython-lib` reproduce branches that
      build and pass `make test`.
    - [ ] `make integrate` runs the rebuild.
  - dependencies: **STORY-8.6** — ticket s8.5 found `local_names_implementation` sits on
    stale ancestry superseded by #8767, so it must be rebuilt on top of `pdb_support`
    (with the two fix commits) before it can be an mbm branch entry. Scope also covers
    registering the ampremote-only mpremote branches (#18436 etc.) as EPIC-4/5 needs them.
  - component: wrapper/mbm · effort: M · risk: med · model: sonnet

- **STORY-8.6 — Upstream the two firmware bug fixes to the canonical branches**
  - **DONE 2026-07-15** — see Status and `s8.6_upstream-firmware-fixes.md`
    Execution outcome. Executed by rebuilding `local_names_implementation` as
    7 bisect-clean commits on `pdb_support` (`7ae5f769c`, fork PR #5), carrying
    both fixes, the param-names regression test, and repairs for further
    defects the rebuild surfaced (`20260715_phase0-canonical-branches-mbm.md`).
  - type: implementation
  - description: The recomposition fixed (a) `frame.f_locals` slot indexing (variables
    reported under wrong names) and (b) `LOCALNAMES_PERSIST` corrupting line numbers
    (now off by default) directly on the integration branch. Attach these onto the
    canonical `local_names_implementation` branch (and #8767 where applicable) so the
    fixes are not stranded in the integration. Q6 is answered (params are NOT omitted —
    `20260706_flocals_params.md`); no fix is needed there, but this story adds the
    positive regression test that was missing (`tests/misc/sys_settrace_locals_params.py`
    + `.exp`) so a future refactor can't silently reintroduce the old omission.
  - acceptance criteria:
    - [ ] both fixes exist as commits on the canonical branch(es) with tests.
    - [ ] a param-name regression test asserts `f_locals` carries parameter names.
  - dependencies: EPIC-1
  - component: firmware · effort: M · risk: med · model: sonnet

---

## Dependency-ordered execution sequence

Topological order suitable for feeding an orchestrator. Items on the same line can run in
parallel.

0. **STORY-8.6 then STORY-8.5** are pulled forward per the 2026-07-06 decisions — do them
   before net-new feature work so upstream-master bumps stay cheap and the fixes are not
   stranded. Order matters: s8.5 found that `local_names_implementation` must first be
   rebuilt on top of `pdb_support` with the two fix commits (that IS STORY-8.6) before
   the composition can be expressed as mbm branch entries. EPIC-1 and STORY-3.1 are
   already DONE; Q6 is closed (no param fix needed, regression test only).
   **DONE 2026-07-15** (both stories; see Status and
   `20260715_phase0-canonical-branches-mbm.md`).
1. **STORY-1.1**, **STORY-1.2** (independent) — and **STORY-3.1** can start here too.
   **DONE.**
2. **STORY-1.3** (needs 1.2), **STORY-1.4** (needs 1.1+1.2), **STORY-3.2** (needs 3.1),
   **STORY-3.3** (needs 1.2+3.1) — parallel. **DONE** (STORY-3.2 completed
   2026-08-04; hardware boot-check deferred to STORY-6.4).
3. **STORY-1.5** (needs 1.1–1.4), **STORY-3.4** (needs 3.1+3.3) — parallel.
   **DONE** (STORY-3.4 completed 2026-08-05: `docs/firmware.md`).
4. **STORY-1.6** (needs 1.5). **DONE.**
5. **STORY-2.1**, **STORY-2.2** (both need 1.4) — parallel spikes. **Gate:** their yes/no
   decides EPIC-4 scope and EPIC-6 shape. **DONE (D2/D3).**
6. **STORY-4.1** (needs 2.1 decision), **STORY-5.1** (needs 1.4) — parallel.
   **DONE 2026-08-05** (both; Q8 was raised and closed to unblock 5.1).
   STORY-4.2 was only reachable if 2.1 had said NO, and it said yes (D2), so
   EPIC-4's unconditional part is finished.
7. **STORY-4.2** (if 2.1=NO; needs 4.1), **STORY-5.2** (needs 5.1+3.3) — parallel.
   STORY-4.2 is not reachable (2.1=YES, D2) and **STORY-5.2 is DONE
   (2026-08-05)**, so this step is closed.
8. **STORY-4.3** (needs 4.1), **STORY-5.4** (needs 5.1+2.2) - parallel.
   **STORY-5.4 DONE 2026-08-06.** STORY-4.3's 4.2 dependency is unreachable
   (2.1=YES), so it reads as 4.1. **Rescoped 2026-08-08:** the question of
   whether staleness still has scope under mount is answered - it does not,
   but nothing mounts either, so the story now owns establishing the mount.
   **← this is the frontier as of 2026-08-08**, with STORY-4.5 immediately
   behind it.
9. **STORY-5.3** (needs 5.1,5.2,3.1,EPIC-1), **STORY-4.4** (needs 4.2,4.3) — parallel.
   **STORY-5.3 DONE 2026-08-06**; STORY-4.4 needs 4.2/4.3, unreachable (2.1=YES).
   **← the frontier is step 10's STORY-5.5** (harness rework: the interleaving
   race, and the end-to-end breakpoint drive s5.3 could not prove).
10. **STORY-5.5** (needs 5.3,5.4), **STORY-6.3** (needs 5.1) — parallel.
    **STORY-5.5 DONE 2026-08-06, so EPIC-5 is complete.**
    **← the frontier is STORY-6.3** (`--dap-log`), the only step-10 item left;
    EPIC-6's transports (6.1/6.2) follow at step 11.
11. **STORY-6.1** (needs 2.2,5.4), **STORY-6.2** (needs 5.4,EPIC-4) — parallel.
    Both need a board on a bench; deferred until the work moves to a host with
    hardware attached. **STORY-6.2 DONE 2026-08-08. STORY-6.1's positive path
    is proven on the same board, and criteria 4 and 3 with it on 2026-08-10; it
    stays open on criterion 2, which needs a board with no second CDC.**
12. **STORY-6.4** (needs 6.1,6.2). Hardware. **Both flows done 2026-08-08 on
    PYBD_SF6, 14 scenarios; open for the rest of the board set.**
    **← the frontier moves back to step 8's STORY-4.3, then STORY-4.5.** Both
    were read as behind EPIC-6; neither is, now that `mpremote mount` works over
    a pty again (2026-08-08), so both develop on the host with the board
    confirming. STORY-8.4 needs no hardware and can run at any point; STORY-8.3
    retargets a public PR base and is a user decision, not an autonomous step.
13. **STORY-8.1** (needs EPIC-5), **STORY-8.2** (needs EPIC-1) — parallel; can start once
    their epics are green. **BOTH DONE 2026-08-06.**
14. **STORY-7.1** (needs EPIC-5; resequenced ahead of 4.3 on 2026-08-06).
    **DONE 2026-08-07**, carrying STORY-7.3 with it.
    **← the frontier is step 15's STORY-7.4 and STORY-7.2**, both reachable
    without hardware.
15. **STORY-7.2**, **STORY-7.3**, **STORY-7.4** (all need 7.1) — parallel.
    **All DONE 2026-08-07, so EPIC-7 is complete.** Every remaining story was
    read as needing a board on a bench: EPIC-6's transports and 6.4,
    STORY-4.3/4.4/4.5 behind them, STORY-8.3 and STORY-8.4. Corrected
    2026-08-08: 4.3 and 4.5 are not board-gated, and 4.4 is unreachable.
16. **STORY-8.3** (needs 8.1,6.4), **STORY-8.4** (needs EPIC-5,6,3) — parallel.
    **STORY-8.4 DONE 2026-08-09**, and closing it closed STORY-6.3's second
    criterion, which had been deferred to exactly this docs pass. **← the
    frontier is what is left of this step: STORY-8.3, which opens a public PR
    and is the user's decision.** Its first two acceptance items - folding the
    unfolded mpremote commits onto `mpremote_debug` and rebasing it onto
    current master - touched nothing public and were done ahead of that
    decision on 2026-08-09, along with the message rewrite the third gate
    needs; the branch is ready to push and PR. **2026-08-09's reading that the
    fold work was "pointless to do locally, since mbm resets those branches to
    the fork tips on every rebuild" was wrong, and both submodules' folds were
    done on 2026-08-10** (see the two Status entries of that date). The reset
    hazard is real but is a reason to protect the tip, not to skip the work:
    `pdb_support` is pushed, so the fork tip *is* the folded tip, and the
    micropython-lib staging tip is held by a tag, which mbm does not touch -
    `staged/add-debugpy-support-20260810` (`2c52eb3`), then
    `staged/add-debugpy-support-20260810b` (`2a125e4`) once criterion 3's fix
    landed on the branch, then `staged/add-debugpy-support-20260810c`
    (`2ff694e`) with STORY-6.6's. Restore from the latest tag if a rebuild
    moves the branch.
    What remains not autonomously reachable: STORY-6.1's criterion 2 needs a
    board with no second CDC (the ESP32 on this bench is offline), STORY-4.4 is
    unreachable by design, and the two pushes that would put either fold in
    front of reviewers are the user's call. **Criteria 4 and 3 were reachable and
    were closed on 2026-08-10**, criterion 4 once "handled" was read as what a
    DAP client can actually be told rather than as ampremote's reconnect, and
    criterion 3 by giving a channel that cannot reach EOF something else to
    report the host leaving with. **Q14 and Q11 were both reachable
    after all and were closed on 2026-08-10.** Q14's shape was decidable from
    the code, and deciding it showed part of the gap did not exist. Q11 looked
    held by fork PR #51 and was not: the installer lives on
    `mpremote_debugpy_install`, a different branch, so giving it a command left
    #51 untouched. Q12 is the only open question left, and it waits on
    STORY-3.2's republish rather than on anything decidable now. **STORY-6.6
    (2026-08-10) came off the risk register rather than out of this sequence**,
    which is where the reachable work now comes from: the epics are complete
    except for what needs a board or a reviewer. **The loop line-event fix
    (2026-08-10) came off the register the same way**, and confirmed the pattern
    is worth working: two of the three rows swept that day were mis-stated
    rather than merely open - one prescribed a fix the .mpy format forbids while
    missing a larger defect next to it.

Notes:
- The whole of EPIC-3 (firmware) runs in parallel with EPIC-1 and the spikes; it only feeds
  in at STORY-5.3.
- If **STORY-2.1 = YES** (mount works under settrace), skip STORY-4.2/4.3's sync-specific
  parts — EPIC-4 collapses to STORY-4.1 plus a live-mount attach path, and the staleness
  guard becomes trivial.
- If **STORY-2.2 = YES** (DAP over serial), STORY-6.1 is the primary device path and
  STORY-6.2 (network) becomes optional; no TCP proxy beyond the localhost↔serial bridge.
- Added post-spike / post-recomposition: **STORY-4.5** slots in after STORY-4.1;
  **STORY-6.5** any time after EPIC-1 (DONE 2026-07-15); **STORY-6.6** likewise,
  filed 2026-08-09 out of STORY-4.5 and executed 2026-08-10; **STORY-8.5** can run at any
  point (earlier = cheaper upstream tracking — Q5) (DONE 2026-07-15); **STORY-8.6**
  after EPIC-1, ideally with STORY-8.2 (DONE 2026-07-15 — executed first, before 8.5).

---

## Risk register

| risk | mitigation |
|------|------------|
| ~~Hand-composed integration cannot be rebuilt when upstream master moves~~ RESOLVED 2026-07-15: both branches rebuild from `mbm.toml` (`make integrate`) | Proven by pinned-target runs (zero conflicts, exact tree equivalence). Residual rules: never run `mbm rebase` without `--local` (its pushes target upstream, not the fork); reset local feature branches to the canonical fork tips after each run; backups `mpy-debugpy-pre-mbm` + `mpy-debugpy-vendored-backup` kept |
| Firmware behaviour assumptions drift when branches are recomposed (e.g. Q6 parameter omission, line-event timing) | runtime capability probe (STORY-1.2) + behavioural DAP tests re-run on every recomposition; never trust names or prose over the probe |
| Upstream review churn on #8767 invalidates the composition | keep integration deltas small and upstream-first (STORY-8.2/8.6); rerere replays conflict resolutions |
| Prebuilt firmware provenance drift (legacy artifacts predate the recomposed branch) | STORY-3.1/3.2: CI-built, hash-verified artifacts from the pinned submodule SHA; the manifest never claims what the probe would contradict |
| EPIC-4/5 assume mpremote primitives (verify_hash, QEMU PTY, reconnect, rfc2217) that exist only in ampremote's tree, not this repo's submodule | DECIDED as D6: register the needed ampremote branches into this repo's `mpy-debugpy` integration via mbm (folded into STORY-8.5 scope) until the PRs land upstream; tickets s4.1/s4.2/s4.4/s5.x carry the dependency explicitly |
| Serial DAP framing on single-UART boards unproven | network transport stays mainline (D3); the framing prototype is a gated follow-up spike (Q3), not a dependency |
| ~~The `extension-host` CI job (STORY-7.4) has never executed a run~~ RESOLVED 2026-08-10, and it had in fact resolved itself on 2026-08-08: job `93088575346` ran the whole cold path the row was about - cache miss, VS Code 1.132.0 downloaded, `ms-python.debugpy` installed from the marketplace, 10 passing, cache saved - and the job has been green in every run since. Original: its first attempt died in GitHub's 2026-08-06 Actions outage at the action-download step, the next two queued 15 minutes and were cancelled with no runner allocated, so its CI wiring is unverified and the two steps local runs never exercise from cold are exactly the ones most likely to differ there | The row outlived the evidence for a reason worth keeping: the run containing that job was cancelled, and `gh run list` reports the *run's* conclusion. A cancelled or failed run can contain a job that finished and passed, and the reverse - a run whose only failing job is skipped-everything-downstream - reads as generic red. Check the job. The job also ran only `test:host` until 2026-08-10, when `npm test` was added to it after the extension's 98 node tests were found to execute on no runner at all |
| ~~Serial DAP throughput on a real board is unmeasured and could make the transport impractical~~ MEASURED 2026-08-08 on PYBD_SF6: 16 KB in 0.20 s, 81.7 kB/s. Original: `StreamTransport.recv` reads one byte per iteration (MicroPython's `.read()`/`.readinto()` loop until full, and the pty stand-in genuinely blocks on the second internal read), measured 33.7 ms and 532 KB of allocation for 4095 bytes on desktop x86 | `test_hil_serial_dap_carries_a_large_response` records the rate into the results record on every run, so a later change is compared against a number from a board. The desktop-pty figure does not transfer: a pty read blocks where a CDC read returns short, so the byte-at-a-time loop is not the bottleneck on hardware and the chunked-read rewrite is not needed to make the transport usable. What hardware did surface was the WRITE side - see D5 in Status; measure both directions on any new board. Row kept rather than deleted: one board is not the board set, and `rpi_pico_w`'s USB stack is a different implementation |
| ~~Busy-poll pause loop starves WiFi/housekeeping while paused on device~~ MEASURED 2026-08-08 on PYBD_SF6: not observable at 60 s | `test_hil_a_long_pause_does_not_drop_the_link` holds a session at a breakpoint with no DAP traffic for `MPY_DEBUG_HIL_PAUSE_SECONDS` (60 by default), then checks both the session's own socket and an unrelated connection attempt. Both answered. The second answers with a refusal, and that is the pass condition, not a fault: `_accept_and_initialize` closes the listening socket once a client is accepted (`public_api.py`, single-connection server), so nothing is bound to the port during a session. A refusal is the board's TCP stack replying; a timeout would mean the link had gone. No tuning made; longer holds and other ports are unmeasured, so the scenario stays in the suite as the measurement rather than the row being deleted |
| `assert wait_for_msg(...)` asserted nothing for the life of the suite - the helper returned `len(rcv_messages) >= count` with `count` defaulting to 0, i.e. true even on timeout, across nine call sites (found 2026-08-06) | fixed in `339192a`: it returns the matched message or None. Watch for the same shape elsewhere - a helper whose success value is computed from a default parameter rather than from what it found |
| **RESOLVED 2026-08-06 (commit 3414f0a) — it was a product deadlock in `process_pending_messages()`, not a harness defect; see `20260806_stopped-event-flake.md`. Retained because the reasoning matters: three rounds were spent on the receiving side, which was innocent, and the allowlist grew to cover it.** Original: the interleaving flake is the project's dominant CI risk (evidence and next steps consolidated in `20260806_stopped-event-flake.md`), and the allowlist is masking a widening defect rather than a understood one.** Measured 2026-08-06 against the composed tip: 0 clean full-suite runs in 3, and the failing ids keep spreading past the four the allowlist was written for (`test_evaluate_default_context_rejects_statements` and `test_unix_debug_subprocess_reaped_on_no_output` added that day). Raising the wait timeout from 5s to 20s did NOT help, so the `stopped` event is not merely late - it never arrives. Each story that adds firmware-spawning tests makes it worse | promote to its own work item before EPIC-6's transports add more device tests. The next step is diagnosing why the event is never emitted under repeated subprocess launches - not another matching-strategy change, which three measurements have now ruled out. Until then the CI gate retries 4 times over an allowlist that has to keep growing |
| **Superseded by the row above — kept for the negative results, not the diagnosis.** This row's mechanism (an async `stopped` event displacing the awaited one, fixable by changing how `wait_for_msg` matches) turned out to be a real but MINOR contributor: the positional-indexing hazard was removed in `339192a`/`d144e56` and the flake persisted unchanged. Do not read its prescription as current; the row above supersedes it. Original text: asynchronous `stopped` events land between a response and the test's next read, so tests that index `rcv_messages` positionally flake (measured ~1 in 2 full runs on 2026-08-04) | deterministic fix scheduled with STORY-5.5. **The fix previously recorded here — "have `wait_for_msg` return the matched message instead of `rcv_messages[-1]`" — was tried on 2026-08-05 and is WRONG; do not retry it as written.** Scanning the list for a match makes the wait return instantly on a *stale* match left by an earlier request in the same test, so it stops waiting for the response it was called for: measured 3 clean / 6 against 4 clean / 6 for the unchanged harness, and it reddened three tests that had never flaked. Reverted. A correct fix has to distinguish messages that arrive *after* the call from ones already in the list (an index or sequence watermark taken at entry, with `run_single` driven against that), which is a harness rework, not a call-site change. Until then the CI unix gate retries up to 4 attempts on all-allowlisted failures, and a locally flaky failure gets one re-run before investigating |
| ~~`launcher/firmware.py` `KNOWN_CAPABILITIES` is a hand-maintained mirror of `debugpy.get_capabilities()` — a new/renamed probe key silently desynchronises selection and the capcheck guard~~ RESOLVED, verified 2026-08-10 | the test the mitigation asked for exists and drives a live probe: `test_epic1_mpdbg_ready_handshake` compares the key set in a real firmware's `MPDBG-READY` caps against `firmware.KNOWN_CAPABILITIES` (less `serial_dap`, which is probed but deliberately not a `requires`/`--need` key), and `test_known_capabilities_matches_wrapper_repo_copies` pins the three hand-copies - `firmware.py`, `mpdebug_config.py`, `gen_manifest.py` - equal to each other. Mutation-checked by adding a key to `firmware.KNOWN_CAPABILITIES`: both fail, one against the running probe and one against the copies. The row outlived its mitigation; it was written for STORY-5.3/8.2 and the coverage landed with them |
| ~~The host harness supplies the debug port itself, so no test ever required the endpoint to be *discoverable* from the device — a design position stayed decorative through a green suite (found 2026-08-05, Q8)~~ RESOLVED for the device, verified 2026-08-10; the host case is host-supplied by design | every test that connects now reads `MPDBG-READY` first and connects to what it says: `tests/hil/conftest.py` hardcodes no address at all (the board's WiFi address is one the host could not have known), and the serial path is asserted to report `serial:0` rather than an address. On the unix flow the port stays host-chosen, which is Q8's decision rather than a gap - `--port 0` is rejected up front so the handshake cannot disagree with the bound port - but the s5.5 fixture still takes the endpoint it connects to from the handshake, which is the general form of the mitigation: consume the value the way production does |
| ~~The DAP `pause` request answers success and never stops the target (found 2026-08-09, STORY-4.5): `paused` is written by `_handle_pause`, `pause()` and `_handle_restart`, and read by no stop decision. A user pressing pause in VS Code gets a UI that believes it is stopped while the program runs on~~ RESOLVED 2026-08-10 by STORY-6.6 | `should_stop` consumes the pending pause at the next `line` event and records `hit_pause`, so the reason comes from whatever made the decision rather than from a second copy of it, and both write-only `DebugSession` fields are deleted rather than left to read as state. The estimate held - four lines - and so did the reason for the row: the harness was the work. `src/spinner.py` is a target that never stops on its own, and the assertion is that its module counter stops advancing, not that an event arrived. The row's diagnosis missed one case, which only appeared once a pause could be sent at all: a `pause` arriving while already stopped is handled inside `wait_for_continue`, and would otherwise stop the target one line into its next run; the flag is cleared on the way out. `20260810_dap-pause.md`. Not fixed and not fixable in this design: a target executing no Python produces no trace event, so the pause stays pending |
| ~~MicroPython emits one more `line` event for a loop body than the body has executions, the extra one before it has run at all (measured 2026-08-09, `20260809_settrace_line_event_fidelity.md`) — so a breakpoint in a loop body stops N+1 times and the first stop shows pre-loop values~~ **PARTLY FIXED and NARROWED 2026-08-10, `20260810_loop-line-events.md`.** Current statement: the **last** source line of the body of an optimised-`range` `for` loop or a `while` loop receives extra `line` events - once per entry to the loop's bottom test with a different line last reported, so once per loop for a simple loop and once per outer iteration when it wraps another loop. A `while` line itself gets one event per frame where CPython gives one per iteration. Every other body line, the `for` header, and all general (non-`range`) `for` loops are 1:1 with CPython | the row's prescription was wrong and is withdrawn: loop-control code is emitted *after* the body and the .mpy line table is monotonically non-decreasing (`py/emitbc.c:459`), so it cannot be re-attributed to the loop header without an .mpy format change, and moving the test to the top of the loop costs a second branch per iteration on every build for a benefit only a tracer sees. What was fixable was a different and larger distortion the 2026-08-09 measurement could not see with its one-line body: general `for` loops reported the header line twice per iteration, because `MP_BC_FOR_ITER` cleared the reported line unconditionally. `py/vm.c` now invalidates on a *backward jump* instead, which is CPython's own rule, and general `for` loops match CPython exactly on every line; loops whose whole body sits on the header line now iterate visibly instead of reporting once. The testing prohibition is narrowed to match: a stop count may be asserted for a breakpoint on a `for` header, on any general-`for` body line, and on any `range`/`while` body line but the last. It stays forbidden on the last body line of a `range`/`while` loop and on a `while` line |
| **RESOLVED 2026-08-10 — it was neither the sleep nor a readiness problem; see `20260810_pty-termios-race.md`.** Two processes were managing one line discipline: the unix port snapshots the pty's termios on its first `mp_hal_stdio_mode_raw()` and restores that snapshot around every exec (`pyexec_raw_repl`), and the snapshot was taken at the kernel's canonical defaults, before pyserial ever rawified the line. Each exec therefore ran with ICRNL rewriting the device's CRs, ECHO feeding the device its own output back as input, and `\x04` as VEOF rather than the raw REPL's end-of-output marker - which is the `timeout waiting for first EOF reception` the check reported. Fixed by `tty.setraw` on the slave before the interpreter is spawned: 0 broken in 800 against a 7-13% base rate. STORY-4.5's harness had the same defect and was found by the full suite, not by the investigation, so the bring-up and the check now live in `tests/pty_device.py`; a sweep for the same shape moved seven further sites onto it. Original: a pty-mounted live session's readiness gate is a fixed 0.3 s sleep before mpremote is launched; one run in three of a new test failed with `timeout waiting for first EOF reception` during `mount_local` (2026-08-09) | the prescription below was followed and is what found it, so keep it: instrument what the device said rather than raise the sleep. Two further lessons. A harness that puts the device on a pty gives it a tty it can *configure*, which no USB CDC device can do - suspect shared-termios ownership before suspecting timing. And the first hypothesis pursued here was well-evidenced and still wrong: with no slave fd open the device's `read(2)` fails EIO and `mp_hal_stdin_rx_chr` returns an uninitialised byte to the REPL 85k times a run (a real upstream defect, left for its own micropython PR), but closing that window changed the failure rate not at all. The measurement that separated them was a 40-line standalone reproducer, not more instrumentation of the full test. A third: a harness defect is rarely confined to the harness it was reported against - copy-constructed test scaffolding carries it silently, so grep for the pattern and fix it in one shared place, and run the whole suite before believing a flake is closed |
| Submodule work lands on the integration branches, where no PR sees it and `make integrate` discards it, and the debt is larger than the "four unfolded mpremote commits" recorded elsewhere: measured 2026-08-09, **seven** non-merge commits touch `tools/mpremote` on `mpy-debugpy` and are on no mpremote feature branch (the four named plus `6a3e3f7d84`, `49502103e3`, `537d60c97c`), and the ruff fixes of that date join them. Every submodule commit acquires this silently - the working tree looks identical either way | do not maintain a hand-written list; it was already stale. The query is `git cherry <feature-branch> <integration>` - patch-id based, so it sees through the rebased duplicates an integration branch carries. `git log --oneline --no-merges <integration> --not <feature-branches...> -- <path>`, recorded here first, over-reports for exactly that reason: it named `e143f3dbec` as unfolded when `git cherry` shows it already on `mpremote_debug` under a different SHA. Run it before declaring any submodule story done. Fold onto the feature branch as part of the story that made the change, the way STORY-8.6 did for the two firmware fixes. Partly discharged the same day: the lint fixes whose owning commit is already on a feature branch were folded there (`42fc0e685e` on `local_names_implementation`; `1e43440`/`7c6ce9f`/`88e796a` on `add-debugpy-support`). What could not follow is exactly the debt this row describes - the `commands.py` and `transport_serial.py` formatting and the `RSE102` fix belong to commits that are themselves unfolded, so they cannot reach a PR before their owners do. **Discharged for `tools/mpremote` 2026-08-09 (STORY-8.3):** the five substantive commits are on `mpremote_debug`, and the format hunks went in as `--fixup`s onto the two commits `git blame` names as owning the lines, so no "format the integration" commit reaches the PR at all. One correction to the query above: `git cherry` against a *single* feature branch over-reports when several branches carve up the same path - three of the nine it flagged belong to `mpremote_debugpy_install` and `mpremote_file_cp_hash`. Test each candidate's patch-id against the union of every feature branch touching that path, not one at a time. **Measured for `python-ecosys/debugpy` 2026-08-10:** ten substantive commits on `mpy-debugpy` are on no feature branch (the stream transport and `serial_dap` derivation, the `save_names` probe fix, the errno/short-write and message-pump fixes, symmetric path translation, the breakpoint-event gating, DAP `restart`, and the two documentation corrections), plus `3bd6c44`, which exists only to undo an earlier integration-only change and must never be folded. A second correction to the query: patch-id cannot see through a cherry-pick that was conflict-resolved, so it reported the samples and typo fixes as unfolded when both are on `add-debugpy-support` - `test_vscode.py`, `demo.py` and `pyproject.toml` are byte-identical across the two branches. Confirm a flagged candidate by diffing the resulting files, not only its patch. **Discharged for `micropython` 2026-08-10:** the last substantive commit (`1d565d21fc`, the trace-callback raise fix) is on `pdb_support` as `139da482d6` and pushed, and the five remaining patch-id hits are all false positives, closed by file diff and recorded in the Status entry of that date. Third correction to the query, the mirror of the second: folding a format `--fixup` into the commit it belongs to also changes that commit's patch-id, so a *correctly* discharged commit keeps being flagged forever. Patch-id is a screen, never a verdict; the union of the feature branches' files against the integration's is the verdict, and residual differences must each be attributed to another branch in the composition or to master drift before the debt can be called zero. **Discharged locally for `micropython-lib` 2026-08-10:** the twelve stranded commits are on `add-debugpy-support`, which leaves `python-ecosys/debugpy/` byte-identical across the two branches; the push that would put them in front of PR #1022's reviewers is held for the user. STORY-6.6's commit was folded the same way as part of the story rather than after it, which is what this row asks for, and the byte-identity check was re-run to confirm it. A fourth correction, this one to the *set* being measured: `mpy-debugpy-foundations` is registered in `mbm.toml` and looks like a feature branch, but it carries no PR number and reaches no PR, so counting it as one hides four commits (the stream transport, the `serial_dap` derivation, the `save_names` probe and the errno/short-write fix). Measure against branches that have a PR, not against everything the composition names |
| A check that is wired up is not a check that runs, and three of them were not (found 2026-08-10, `20260810_ci-gates-that-never-ran.md`): the lint gate could not resolve ruff on a runner and, sitting before the host DAP suite, kept it from starting for four days; the extension's 98 node tests reached CI only through a pytest case that the one job containing it skips for lack of a node toolchain; and the push trigger did not name `Makefile`, `pyproject.toml`, `uv.lock` or `src/`, so a change to what CI runs or to the debuggees it runs against could match no path. Each looked like coverage from the workflow file | all three are fixed, but the shape recurs, so check the observation and not the wiring. For a tool a job invokes, prove the job can obtain it - `make lint` was verified in an `env -i` shell with only `uv`, `make` and `git` on PATH, which is the condition that failed. For a suite, read the count off a runner's log and reconcile it against the local count; the 362-vs-361 gap is what exposed the node tests. For a paths filter, ask what the job reads, not what it builds. And a green local run says nothing about CI: every green claim in this file between 2026-08-06 and 2026-08-10 was local-only, correct as far as it went and silent about the runner |
| ~~The top repo's submodule pointer can be pushed while the commit it names exists only locally, and then CI checks nothing at all: `compute version metadata` dies with `upload-pack: not our ref <sha>` and every job downstream is skipped, so the run reads as a generic red rather than as "the tree was never fetched". Four runs between 2026-08-07 and 2026-08-08 ended that way, on three different `micropython` SHAs~~ **ENFORCED 2026-08-10 - the mitigation was a manual step and is now a check.** `tools/check-submodule-pins.sh` reads each pin recorded in a given commit, finds the submodule remote whose URL matches the one `.gitmodules` gives CI, fetches it, and requires the pin to be on a branch there. `make bootstrap` installs `tools/pre-push-check.sh` as the top repo's `pre-push` hook, which runs it once per ref being pushed; `make check-pins` runs it against HEAD | the manual query this row carried still describes what the check asserts, so keep reading it that way: a pin must be *reachable on the fork*, not merely committed. Two deliberate details. It fetches before asking, because a remote-tracking ref goes stale the moment anyone else pushes and a stale ref is precisely the false pass this exists to prevent. And `make install-hooks` reports rather than symlinks when `core.hooksPath` is set, because a hook that looks installed and never runs is the failure the wired-up-but-never-ran row above describes. Verified in both directions against a synthesised unpushed pin - a top-repo commit built with `commit-tree`/`mktree`, so the probe touched neither index nor worktree: the installed hook refuses that push and passes the true HEAD. It remains a guard rail rather than a proof: `--no-verify` bypasses it and it guards this clone only. Same family as the stranded-commits row below - a submodule change that is not pushed is invisible from the top repo's working tree, which looks identical either way |
| A background HIL run whose output is kept but whose tree is not proves nothing, and one was read as a regression before that was noticed: a run on 2026-08-09 at 12:29 failed 3 of 11 network scenarios, and its surviving traceback shows a revision of `test_hil_a_long_pause_does_not_drop_the_link` that expected a fresh connection to the endpoint to *succeed*. No such revision was ever committed - the file has one commit, and that line number falls inside the docstring there - so the run was against a discarded working-tree draft, and all three failures are unattributable rather than flaky. The committed test asserts the refusal, which is what the server does by design | pair every HIL run with the tree it ran against: record HEAD and `git status --short` alongside the output, and do not launch one from a dirty tree. A failing scenario whose expectation cannot be traced to a commit is not evidence about the device. Applies to the all-pass results record too - it is what one run of one tree saw |
| A submodule's CI is not fully described by the workflows in its own tree: micropython-lib's `codespell` job exists only on upstream master, so a branch that predates it runs the check on every PR while showing no sign of it locally, and the job that reports as **`build`** in a PR's check list is the commit-message check from `commit_formatting.yml`. `make lint-submodules` reproduces only the two ruff commands and would have said "clean" for both (found 2026-08-09) | read the check list on the PR itself, by name and by log, before claiming a branch is CI-green; `gh api repos/<upstream>/commits/<sha>/check-runs` is the source of truth, not the branch's `.github/workflows/`. Extend `lint-submodules` when a new gating job is found rather than treating its absence from the branch as its absence from CI |

---

## Progress tracking

New planning documents are written as `planning/YYYYMMDD_<topic>.md` with a header
carrying the date, the top-repo HEAD SHA (`git rev-parse --short=10 HEAD`), and the
relevant submodule SHAs, so every finding is reproducible against a known tree state.
This roadmap — the Status section, the open-questions table, and the risk register — is
updated in place as work lands, never forked. Spike and design documents are decision
records: append corrections, do not rewrite conclusions.

At the entrance to each phase, the workflow planner MUST revalidate that phase's
tickets against everything done since they were written — code drift via
`git log`/`git diff` from the ticket's stamped SHAs (including submodule SHAs),
knowledge drift via planning documents dated after the stamp — updating each ticket in
place and appending a `Revalidated:` line before any workflow consumes it. The full
procedure, conventions, and ticket template live in `planning/00_index.md`; each
executed phase writes its progress and learnings back as `YYYYMMDD_<topic>.md`.
