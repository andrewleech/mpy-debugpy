/**
 * Extension-host layer: what only a running VS Code can prove.
 *
 * The pure/unit layer (handshake, config, command, and an integration test
 * against the real `mpremote debug unix`) lives in `src/test/*.test.ts` and
 * runs under plain `node:test` via `npm test`. This suite runs inside a
 * real Extension Development Host (`npm run test:host`) and answers what
 * that layer cannot: whether `startDebugging` on the real VS Code debug API
 * reaches a stop at the right location, whether the mpremote child is
 * reliably gone by the time a session ends, which of the two correlation
 * paths in extension.ts's `matchesLaunch` did the work, and whether the
 * Dynamic-trigger provider registration is ever actually reached by VS
 * Code's own picker.
 *
 * The workspace under test is this repo's own root (opened by
 * runHostTests.ts), so `.vscode/launch.json`'s "MicroPython: Debug"
 * config is driven exactly as a real user's F5 would drive it, wherever a
 * test doesn't specifically need a different debuggee.
 */

import * as assert from "node:assert/strict";
import * as os from "node:os";
import * as path from "node:path";

import * as vscode from "vscode";

import { listMpremotePids, pidAlive, requireProcTable, waitFor, withErrorMessageSpy } from "./procUtil";

const EXTENSION_ID = "andrewleech.mpy-debugpy";
// The pytest harness's line-choice rule (tests/test_epic1_foundations.py):
// a `line` event fires BEFORE the statement at that line executes, so a
// breakpoint must land on a line whose own execution isn't required to
// observe the stop - `print(...)` at src/target.py:78 (1-based), the same
// line test_epic1_breakpoint_stops_target uses.
const TARGET_BREAKPOINT_LINE_1BASED = 78;
// blocking_target.py's `while running:` header - `running = True` on the
// line above has already run by the time this line's `line` event fires.
const BLOCKING_TARGET_BREAKPOINT_LINE_1BASED = 12;

// `mpremote debug unix`'s own handshake wait defaults to 60s plus a 5s
// margin (extension/src/command.ts), and `.vscode/launch.json` sets no
// shorter `timeout` - so a genuine regression here can legitimately take
// that long to report itself. Bounding the test well above that sum means
// a real failure surfaces as the resolver's own diagnostic, not a bare
// mocha "Timeout exceeded" with no cleanup run.
const RESOLVER_TEST_TIMEOUT_MS = 120_000;

interface DapMessage {
  type?: string;
  event?: string;
  body?: { reason?: string; threadId?: number };
}

/** Tracks the most recent `stopped` event's body for the `debugpy` type. */
function trackStopped(): { dispose(): void; stopped: { reason?: string; threadId?: number } | undefined } {
  let stopped: { reason?: string; threadId?: number } | undefined;
  const reg = vscode.debug.registerDebugAdapterTrackerFactory("debugpy", {
    createDebugAdapterTracker() {
      return {
        onDidSendMessage(message: DapMessage) {
          if (message.type === "event" && message.event === "stopped") {
            stopped = message.body;
          }
        },
      };
    },
  });
  return {
    dispose: () => reg.dispose(),
    get stopped() {
      return stopped;
    },
  };
}

describe("extension-host smoke (STORY-7.4)", function () {
  let folder: vscode.WorkspaceFolder;

  before(function () {
    const folders = vscode.workspace.workspaceFolders;
    assert.ok(folders && folders.length > 0, "no workspace folder open in the test host");
    folder = folders![0];
    // A breakpoint left behind by a run that crashed before its own
    // `finally` would otherwise satisfy a later test's "some breakpoint was
    // hit" check for the wrong reason.
    vscode.debug.removeBreakpoints(vscode.debug.breakpoints);
  });

  it("ms-python.debugpy is present in the test host", function () {
    // This is itself under test, not an environment nicety: the extension
    // declares it as extensionDependencies and must be able to rely on it.
    // Absence must fail the suite, never skip it silently.
    const debugpy = vscode.extensions.getExtension("ms-python.debugpy");
    assert.ok(debugpy, "ms-python.debugpy is not installed in the test VS Code build");
  });

  it("the Dynamic-trigger provider is activated and actually reached by VS Code's picker", async function () {
    this.timeout(45_000);
    const ext = vscode.extensions.getExtension(EXTENSION_ID);
    assert.ok(ext, `${EXTENSION_ID} is not present in the test host`);
    // Must run before anything else activates the extension via
    // onDebugResolve, or this assertion is meaningless.
    assert.equal(
      ext!.isActive,
      false,
      "extension already active - this test must run before any startDebugging call"
    );

    let sawDebugpySession = false;
    const startedSub = vscode.debug.onDidStartDebugSession((session) => {
      if (session.type === "debugpy") {
        sawDebugpySession = true;
      }
    });

    try {
      await withErrorMessageSpy(async (errors) => {
        // 'workbench.action.debug.selectandstart' with an explicit type
        // looks up the matching Dynamic provider and calls its `pick()`,
        // which is what fires `onDebugDynamicConfigurations:micropython`
        // and awaits our provider's activation before showing a quickpick
        // of its configs - the same mechanism the "Show all automatic
        // debug configurations" command-palette entry uses for a real
        // user.
        const picked = vscode.commands.executeCommand("workbench.action.debug.selectandstart", "micropython");
        await waitFor(() => ext!.isActive === true, 15_000);

        // Drive the quickpick to completion rather than cancel it: there is
        // no public event for "the quickpick is now showing", so retry the
        // accept command on a bounded poll (not a fixed sleep), stopping as
        // soon as the resolver's own observable effect appears (an error
        // toast, since the provided config carries no target and this repo
        // has no mpdebug.toml) rather than re-firing accept for the whole
        // budget regardless.
        const deadline = Date.now() + 15_000;
        while (errors.length === 0 && !sawDebugpySession && Date.now() < deadline) {
          await vscode.commands.executeCommand("workbench.action.acceptSelectedQuickOpenItem");
          await new Promise((resolve) => setTimeout(resolve, 250));
        }
        // Always await `picked` itself before this scope exits and the
        // error-message spy is uninstalled, whether or not the loop above
        // already observed a result: `picked` settles once VS Code's whole
        // selectandstart flow (including our resolver) is done, and letting
        // it finish outside this scope would misattribute its toast to
        // whichever test runs next.
        await picked.then(undefined, () => undefined);
        assert.ok(
          errors.length > 0 || sawDebugpySession,
          "selectandstart's quickpick never resolved - the Dynamic provider may not have supplied any configs"
        );
        assert.equal(errors.length, 1, `expected exactly one diagnostic, saw: ${JSON.stringify(errors)}`);
      });
    } finally {
      startedSub.dispose();
    }
    assert.equal(sawDebugpySession, false, "no target is configured; a debugpy session should not have started");
  });

  it("F5 against the unix target hits the breakpoint set in src/target.py", async function () {
    this.timeout(RESOLVER_TEST_TIMEOUT_MS);

    const targetUri = vscode.Uri.joinPath(folder.uri, "src", "target.py");
    const breakpoint = new vscode.SourceBreakpoint(
      new vscode.Location(targetUri, new vscode.Position(TARGET_BREAKPOINT_LINE_1BASED - 1, 0))
    );
    vscode.debug.addBreakpoints([breakpoint]);
    const tracker = trackStopped();

    // Answers goal 2: does the `mpyDebugLaunchId` marker extension.ts stamps
    // onto the attach config survive ms-python.debugpy's own resolver, or
    // is `matchesLaunch`'s port-based fallback what actually correlates the
    // session to its child?
    let debugpySessionConfig: vscode.DebugConfiguration | undefined;
    const startedSub = vscode.debug.onDidStartDebugSession((session) => {
      if (session.type === "debugpy") {
        debugpySessionConfig = session.configuration;
      }
    });

    try {
      await withErrorMessageSpy(async (errors) => {
        // The outer call resolves to `false` even on success: the resolver
        // (correctly, per the resolver-cannot-change-type contract) returns
        // `undefined` after redirecting to its own nested `debugpy` session,
        // and VS Code reports that as "this specific request did not start
        // a session" regardless of the nested one. The tracker's `stopped`
        // event below is the real signal.
        await vscode.debug.startDebugging(folder, "MicroPython: Debug");
        assert.deepEqual(errors, [], "no error toast expected on the happy path");
      });

      await waitFor(() => tracker.stopped?.reason === "breakpoint", 30_000);

      assert.equal(
        typeof debugpySessionConfig?.mpyDebugLaunchId,
        "string",
        "mpyDebugLaunchId did not survive ms-python.debugpy's resolver - matchesLaunch's port fallback did the correlating instead"
      );

      const activeSession = vscode.debug.activeDebugSession;
      assert.ok(activeSession, "no active debug session while stopped at the breakpoint");
      const stack = await activeSession.customRequest("stackTrace", { threadId: tracker.stopped!.threadId });
      const topFrame = stack.stackFrames[0];
      assert.equal(topFrame.source.path, targetUri.fsPath);
      assert.equal(topFrame.line, TARGET_BREAKPOINT_LINE_1BASED);

      await vscode.debug.stopDebugging(activeSession);
    } finally {
      tracker.dispose();
      startedSub.dispose();
      vscode.debug.removeBreakpoints([breakpoint]);
    }
  });

  // NOTE on what this test can and cannot prove: on the unix flow,
  // ms-python.debugpy's `disconnect` (sent with `terminateDebuggee: false`,
  // captured via `dapLog` against this exact config) reaches the on-target
  // server's `_handle_disconnect`, which ignores that flag and always calls
  // `disconnect()` -> `channel.close()`. `process_pending_messages`'s
  // `finally` clause then calls `settimeout` on the now-`None` socket,
  // raising, and `mpy_launch_debugpy.py`'s top-level `except Exception`
  // swallows it into a clean `exit(0)` - so the target interpreter (and,
  // once mpremote's blocking stdout read hits EOF, mpremote itself) dies on
  // *any* disconnect, whether or not `terminateChild` in extension.ts's
  // `onDidTerminateDebugSession` handler ever runs. Verified directly:
  // deleting that `terminateChild` call still leaves this test green. So
  // this test cannot attribute the reap to the extension - it only proves
  // the mpremote child never outlives its debug session by more than the
  // bound below, which still catches a hang, an orphaned process, or a
  // correlation failure that leaves nothing to reap it at all.
  it("the mpremote child does not outlive its debug session", async function () {
    this.timeout(RESOLVER_TEST_TIMEOUT_MS);
    requireProcTable();

    const fixtureDir = path.join(folder.uri.fsPath, "extension", "src", "test", "fixtures");
    const targetUri = vscode.Uri.file(path.join(fixtureDir, "blocking_target.py"));
    const breakpoint = new vscode.SourceBreakpoint(
      new vscode.Location(targetUri, new vscode.Position(BLOCKING_TARGET_BREAKPOINT_LINE_1BASED - 1, 0))
    );
    vscode.debug.addBreakpoints([breakpoint]);
    const tracker = trackStopped();

    const config: vscode.DebugConfiguration = {
      type: "micropython",
      request: "launch",
      name: "MicroPython: host-test blocking target",
      target: "unix",
      program: "blocking_target:main",
      cwd: folder.uri.fsPath,
      mpremotePath: "python3",
      mpremoteArgs: ["-m", "mpremote"],
      env: {
        PYTHONPATH: path.join(folder.uri.fsPath, "micropython", "tools", "mpremote"),
        MPY_DEBUG_FIRMWARE:
          process.env.MPY_DEBUG_FIRMWARE ??
          path.join(folder.uri.fsPath, "micropython", "ports", "unix", "build-standard", "micropython"),
        MICROPYPATH: [fixtureDir, path.join(folder.uri.fsPath, "micropython-lib", "python-ecosys", "debugpy")].join(
          ":"
        ),
      },
    };

    const beforePids = new Set(listMpremotePids());

    // Stop the session this test started, not `activeDebugSession`: that is
    // whatever VS Code last focused, and stopping the wrong one leaves the
    // debuggee attached, the child running, and this test timing out for a
    // reason that has nothing to do with what it checks.
    let debugpySession: vscode.DebugSession | undefined;
    const startedSub = vscode.debug.onDidStartDebugSession((session) => {
      if (session.type === "debugpy" && session.configuration.mpyDebugLaunchId) {
        debugpySession = session;
      }
    });
    let childPid: number | undefined;

    try {
      await withErrorMessageSpy(async (errors) => {
        await vscode.debug.startDebugging(folder, config);
        assert.deepEqual(errors, [], "no error toast expected on the happy path");
      });

      await waitFor(() => tracker.stopped?.reason === "breakpoint", 30_000);

      const newPids = listMpremotePids().filter((p) => !beforePids.has(p));
      assert.equal(
        newPids.length,
        1,
        `expected exactly one new mpremote child; before=${[...beforePids]} now=${listMpremotePids()}`
      );
      childPid = newPids[0];
      const pid = childPid;

      // Control: blocking_target.py's main() loops forever once resumed
      // rather than finishing on its own, ruling out that particular
      // confound - confirm the child is still here immediately before we
      // ask for the session to stop.
      assert.equal(pidAlive(pid), true, "the mpremote child exited before the session was even stopped");

      assert.ok(debugpySession, "no debugpy session was started for this launch");
      await vscode.debug.stopDebugging(debugpySession);

      await waitFor(() => !pidAlive(pid), 15_000);
    } finally {
      tracker.dispose();
      startedSub.dispose();
      vscode.debug.removeBreakpoints([breakpoint]);
      // Only reached with the assertion above already failed, so this hides
      // nothing - it stops one bad run from seeding every later one with a
      // stray child holding the debug port.
      if (childPid !== undefined && pidAlive(childPid)) {
        try {
          process.kill(childPid, "SIGKILL");
        } catch {
          // already gone
        }
      }
    }
  });

  it("a command that can't even spawn produces a diagnostic, not a hang or a session", async function () {
    this.timeout(20_000);
    let sawDebugpySession = false;
    const sub = vscode.debug.onDidStartDebugSession((session) => {
      if (session.type === "debugpy") {
        sawDebugpySession = true;
      }
    });
    try {
      await withErrorMessageSpy(async (errors) => {
        // startDebugging's return value is uninformative here: it resolves
        // to `false` on this path AND on the happy path (see the comment in
        // the breakpoint test above), so it can't distinguish success from
        // failure. The error-toast count and the absence of a debugpy
        // session are the real signals.
        await vscode.debug.startDebugging(folder, {
          type: "micropython",
          request: "launch",
          name: "MicroPython: host-test broken command",
          target: "unix",
          program: "target:main",
          mpremotePath: path.join(os.tmpdir(), "mpy-debugpy-host-test-no-such-binary"),
        });
        assert.equal(errors.length, 1, `expected exactly one diagnostic error toast, saw: ${JSON.stringify(errors)}`);
      });
    } finally {
      sub.dispose();
    }
    assert.equal(sawDebugpySession, false, "no debugpy session should start when the mpremote child can't spawn");
  });
});
