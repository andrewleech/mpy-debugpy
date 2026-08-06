/**
 * Extension-host coverage for the target picker (STORY-7.2): the parts of
 * `targetPicker.ts` that need a real VS Code - persisted selection via
 * `workspaceState`, and the status bar item VS Code itself owns. Parsing,
 * the discovery walk, and error shapes are unit-tested against `targets.ts`
 * directly in `src/test/targets.test.ts` and don't need a host.
 *
 * Runs after `e2e.test.js` (mocha loads suite files sorted; "e2e" < this
 * file's name), which probes the "unix" target for real - `activate()`
 * below is idempotent regardless, but tests here that care about a
 * genuinely never-probed target use a name that couldn't collide with it.
 */

import * as assert from "node:assert/strict";
import * as fs from "node:fs";
import * as path from "node:path";

import * as vscode from "vscode";

import type { ExtensionApi } from "../../../extension";
import { SELECTED_TARGET_STATE_KEY, UNPROBED_TEXT } from "../../../targetPicker";
import { withInformationMessageSpy, withErrorMessageSpy, withQuickPickStub } from "./procUtil";

const EXTENSION_ID = "andrewleech.mpy-debugpy";

describe("target picker (STORY-7.2)", function () {
  let api: ExtensionApi;
  // This suite is the only thing that writes SELECTED_TARGET_STATE_KEY, and
  // must leave it as it found it: a later run reading back a selection this
  // suite persisted (rather than made afresh) would poison its own "no
  // selection yet" premises.
  let originalSelection: string | undefined;

  before(async function () {
    const ext = vscode.extensions.getExtension(EXTENSION_ID);
    assert.ok(ext, `${EXTENSION_ID} is not present in the test host`);
    api = (await ext!.activate()) as ExtensionApi;
    originalSelection = api.workspaceState.get(SELECTED_TARGET_STATE_KEY);
  });

  after(async function () {
    await api.workspaceState.update(SELECTED_TARGET_STATE_KEY, originalSelection);
  });

  it("this repo's own missing mpdebug.toml produces an actionable message, not a stack trace", async function () {
    this.timeout(10_000);
    // This repo's root (the test host's workspace folder) genuinely has no
    // mpdebug.toml - the "own default path" the ticket calls out - so this
    // exercises the real no-config case, not a fixture standing in for it.
    await withErrorMessageSpy(async (errors) => {
      await api.targetPicker.promptForTarget();
      assert.equal(errors.length, 1, `expected exactly one diagnostic, saw: ${JSON.stringify(errors)}`);
      assert.match(errors[0], /no mpdebug\.toml found/);
    });
  });

  it("shows 'capabilities unprobed' for a freshly selected target with no handshake yet", async function () {
    this.timeout(10_000);
    const folder = vscode.workspace.workspaceFolders![0];
    const configPath = path.join(folder.uri.fsPath, "mpdebug.toml");
    fs.writeFileSync(configPath, ['[target.never_probed]', 'kind = "unix"'].join("\n"));
    try {
      await withQuickPickStub("never_probed", () => api.targetPicker.promptForTarget());
      assert.equal(api.targetPicker.selectedTarget, "never_probed");
      assert.match(String(api.targetPicker.statusBarItem.tooltip), new RegExp(UNPROBED_TEXT));
    } finally {
      fs.rmSync(configPath, { force: true });
    }
  });

  it(
    "picking a target persists it (to the workspaceState memento, not just the getter), and its " +
      "capability display comes only from the probe - never from its name, even when the name lies",
    async function () {
      this.timeout(15_000);
      const folder = vscode.workspace.workspaceFolders![0];
      // Named after a capability it does not actually claim to have: the
      // adversarial case from the ticket. A real mpdebug.toml is required
      // because promptForTarget's discovery walk is exercised for real here,
      // not stubbed - written transiently at the repo root (the workspace
      // folder discovery starts from) and removed in `finally` either way.
      const configPath = path.join(folder.uri.fsPath, "mpdebug.toml");
      fs.writeFileSync(
        configPath,
        ['[target.unix]', 'kind = "unix"', "", "[target.set_local]", 'kind = "unix"'].join("\n")
      );
      try {
        await withQuickPickStub("set_local", () => api.targetPicker.promptForTarget());
        assert.equal(api.targetPicker.selectedTarget, "set_local");
        // Proves the selection reached VS Code's own storage API, not just
        // a private field with the same shape as the getter.
        assert.equal(api.workspaceState.get(SELECTED_TARGET_STATE_KEY), "set_local");
        // Selecting a target is not itself a probe: no handshake has run
        // for it yet in this suite.
        assert.match(String(api.targetPicker.statusBarItem.tooltip), new RegExp(UNPROBED_TEXT));

        api.targetPicker.reportHandshake("set_local", {
          set_local: false,
          save_names: true,
          settrace: true,
          f_back: true,
        });
        const tooltip = String(api.targetPicker.statusBarItem.tooltip);
        assert.match(tooltip, /read-only/i);
        assert.doesNotMatch(tooltip, /editable/i);
      } finally {
        fs.rmSync(configPath, { force: true });
      }
    }
  );

  it("an explicit launch.json 'target' always wins over the picker's selection", async function () {
    this.timeout(45_000);
    const folder = vscode.workspace.workspaceFolders![0];
    const configPath = path.join(folder.uri.fsPath, "mpdebug.toml");
    // A target the resolver must NOT use: a nonexistent serial device fails
    // fast, so using it (an inverted precedence bug) is distinguishable from
    // the explicit-"unix" launch config succeeding.
    fs.writeFileSync(
      configPath,
      ['[target.picker_bad]', 'kind = "serial"', 'device = "/dev/mpy-debugpy-nonexistent"'].join("\n")
    );
    try {
      await withQuickPickStub("picker_bad", () => api.targetPicker.promptForTarget());
      assert.equal(api.targetPicker.selectedTarget, "picker_bad");

      let debugpySession: vscode.DebugSession | undefined;
      const startedSub = vscode.debug.onDidStartDebugSession((session) => {
        if (session.type === "debugpy") {
          debugpySession = session;
        }
      });
      try {
        await withErrorMessageSpy(async (errors) => {
          await vscode.debug.startDebugging(folder, "MicroPython: Debug");
          assert.deepEqual(errors, [], `expected no error toast, saw: ${JSON.stringify(errors)}`);
        });
        assert.ok(
          debugpySession,
          "launch.json's explicit target: 'unix' should have started a debugpy session " +
            "even with a different target selected in the picker"
        );
      } finally {
        startedSub.dispose();
        // target.py's main() runs to completion in well under this test's
        // budget with no breakpoint set, so the session may already be
        // gone by the time this runs; stopDebugging on a dead session is a
        // harmless no-op either way.
        if (debugpySession) {
          try {
            await vscode.debug.stopDebugging(debugpySession);
          } catch {
            // already gone
          }
        }
      }
    } finally {
      fs.rmSync(configPath, { force: true });
    }
  });

  it("warns about placeholder local names once per target, not on every handshake", async function () {
    this.timeout(10_000);
    await withInformationMessageSpy(async (messages) => {
      api.targetPicker.reportHandshake("set_local", {
        set_local: false,
        save_names: false,
        settrace: true,
        f_back: true,
      });
      api.targetPicker.reportHandshake("set_local", {
        set_local: false,
        save_names: false,
        settrace: true,
        f_back: true,
      });
      const placeholderWarnings = messages.filter((m) => /placeholder/i.test(m));
      assert.equal(
        placeholderWarnings.length,
        1,
        `expected exactly one placeholder-names warning, saw: ${JSON.stringify(messages)}`
      );
    });
  });
});
