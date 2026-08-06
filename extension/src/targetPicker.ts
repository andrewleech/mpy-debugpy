/**
 * Status-bar target picker: lists the named targets from the project's
 * `mpdebug.toml` (via `targets.ts`), persists the pick in `workspaceState`,
 * and displays the probed capability summary for whichever target actually
 * ran a handshake.
 *
 * Capability text comes only from `reportHandshake`'s `caps` argument -
 * this module never derives it from a target's name or `kind`. Before any
 * handshake for a given target, the tooltip reads "capabilities unprobed"
 * for it; `reportSessionStart` clears a target's cached probe as a new
 * session for it begins, so a stale probe from an earlier session (e.g.
 * against different firmware) is never shown as current.
 */

import * as vscode from "vscode";

import { summarizeCapabilities } from "./config";
import { discoverTargets } from "./targets";

export const SELECT_TARGET_COMMAND = "mpy-debugpy.selectTarget";
export const SELECTED_TARGET_STATE_KEY = "mpy-debugpy.selectedTarget";
const STATUS_BAR_PRIORITY = 100;
export const UNPROBED_TEXT = "capabilities unprobed";

interface Probe {
  caps: Record<string, boolean>;
}

/** A probe plus the target name it was recorded against (undefined = no target attribute and no picker selection). */
interface NamedProbe {
  targetName: string | undefined;
  probe: Probe;
}

interface TargetQuickPickItem extends vscode.QuickPickItem {
  targetName: string;
}

export class TargetPicker implements vscode.Disposable {
  private readonly bar: vscode.StatusBarItem;
  private readonly disposables: vscode.Disposable[] = [];
  // Keyed by target name; a session run with no explicit target (neither a
  // launch-config attribute nor a picker selection) is tracked separately
  // rather than under a sentinel string, since a target could legitimately
  // be named anything.
  private readonly namedProbes = new Map<string, Probe>();
  // The most recent handshake regardless of target name, so a session run
  // via an explicit launch-config `target` (with no picker selection made)
  // still has something honest to show - see `render()`.
  private lastProbe: NamedProbe | undefined;
  private readonly namedWarned = new Set<string>();
  private defaultWarned = false;

  constructor(
    private readonly context: vscode.ExtensionContext,
    private readonly channel: vscode.OutputChannel
  ) {
    this.bar = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, STATUS_BAR_PRIORITY);
    this.bar.command = SELECT_TARGET_COMMAND;
    this.disposables.push(
      this.bar,
      vscode.commands.registerCommand(SELECT_TARGET_COMMAND, () => this.promptForTarget())
    );
    this.render();
    this.bar.show();
  }

  /** Exposed for tests to read `.text`/`.tooltip` directly; not used by extension.ts. */
  get statusBarItem(): vscode.StatusBarItem {
    return this.bar;
  }

  /** The persisted pick, surviving a window reload; undefined if none has been made. */
  get selectedTarget(): string | undefined {
    return this.context.workspaceState.get<string>(SELECTED_TARGET_STATE_KEY);
  }

  private setSelectedTarget(name: string): void {
    void this.context.workspaceState.update(SELECTED_TARGET_STATE_KEY, name);
    this.render();
  }

  /**
   * Opens a QuickPick over the parsed `mpdebug.toml` targets. A missing or
   * malformed file, or no open workspace folder, produces an actionable
   * message and returns - it never throws, and never blocks picking a
   * target from happening again after the file is fixed.
   *
   * Discovery always starts from `workspaceFolders[0]`: this command isn't
   * tied to any one launch config, so it can't in general know which
   * `cwd` a later F5 will resolve against (a multi-root workspace, or a
   * launch config with its own `cwd`, may resolve a different
   * `mpdebug.toml`). The QuickPick title names the file actually found so
   * a mismatch is visible rather than silent.
   */
  async promptForTarget(): Promise<void> {
    const dir = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
    if (dir === undefined) {
      vscode.window.showErrorMessage("MicroPython: open a workspace folder to pick a debug target.");
      return;
    }

    const result = discoverTargets(dir);
    if (!result.ok) {
      this.channel.appendLine(result.error.message);
      vscode.window.showErrorMessage(`MicroPython: ${result.error.message}`);
      return;
    }
    if (result.targets.length === 0) {
      vscode.window.showInformationMessage(`MicroPython: ${result.path} defines no [target.<name>] entries.`);
      return;
    }

    const items: TargetQuickPickItem[] = result.targets.map((t) => ({
      label: t.name,
      description: t.kind,
      targetName: t.name,
    }));
    const picked = await vscode.window.showQuickPick(items, {
      placeHolder: "Select the MicroPython debug target",
      title: `MicroPython debug target (from ${result.path})`,
    });
    if (picked) {
      this.setSelectedTarget(picked.targetName);
    }
  }

  /**
   * Called by the resolver just before it spawns `mpremote debug` for
   * `targetName` (the value it is about to pass - an explicit launch-config
   * attribute, the picker's selection, or undefined for neither). Drops any
   * cached probe recorded under that same key, so the tooltip reads
   * "capabilities unprobed" until this session's own handshake lands rather
   * than presenting a previous session's probe (e.g. against different
   * firmware) as current.
   */
  reportSessionStart(targetName: string | undefined): void {
    if (targetName === undefined) {
      if (this.lastProbe?.targetName === undefined) {
        this.lastProbe = undefined;
      }
    } else {
      this.namedProbes.delete(targetName);
      if (this.lastProbe?.targetName === targetName) {
        this.lastProbe = undefined;
      }
    }
    this.render();
  }

  /**
   * Records a session's probed capabilities, called by the resolver once a
   * handshake arrives. `targetName` is the value actually passed to
   * `mpremote debug` (an explicit launch-config attribute, the picker's
   * selection, or undefined when neither applied and the command fell back
   * to its own default target resolution).
   *
   * Refreshes the status-bar tooltip - directly when `targetName` matches
   * the current picker selection, and as the "most recent session" fallback
   * otherwise (see `render()`) - and shows a one-shot notification per
   * target name the first time `save_names` is observed false for it, the
   * "you are seeing placeholder names" surprise, not repeated on every
   * later handshake for the same target.
   */
  reportHandshake(targetName: string | undefined, caps: Record<string, boolean>): void {
    const probe: Probe = { caps };
    if (targetName !== undefined) {
      this.namedProbes.set(targetName, probe);
    }
    this.lastProbe = { targetName, probe };
    this.render();

    if (!caps.save_names) {
      const alreadyWarned = targetName === undefined ? this.defaultWarned : this.namedWarned.has(targetName);
      if (!alreadyWarned) {
        if (targetName === undefined) {
          this.defaultWarned = true;
        } else {
          this.namedWarned.add(targetName);
        }
        vscode.window.showInformationMessage(
          `MicroPython (${targetName ?? "default target"}): local variable names are placeholders ` +
            "(local_N) - this firmware does not support save_names."
        );
      }
    }
  }

  /**
   * The status-bar text always names the picker's own selection (or the
   * generic label when nothing is picked); the tooltip's capability line
   * prefers a probe recorded for that same selection, falling back to the
   * most recent handshake of any name - covering the common case where a
   * launch config sets `target` explicitly and no pick has ever been made,
   * so that session's probe is still shown, honestly labelled with its own
   * target name rather than folded into the current (unrelated) selection.
   */
  private render(): void {
    const selected = this.selectedTarget;
    this.bar.text = `$(debug-alt) ${selected ?? "MicroPython"}`;

    const namedProbe = selected !== undefined ? this.namedProbes.get(selected) : undefined;
    if (namedProbe) {
      this.bar.tooltip = `MicroPython debug target: ${selected}\n${summarizeCapabilities(namedProbe.caps).join("\n")}`;
      return;
    }
    if (selected === undefined && this.lastProbe) {
      const label = this.lastProbe.targetName ?? "the default target";
      this.bar.tooltip =
        `MicroPython debug target: none selected via the picker (click to choose)\n` +
        `most recent session used '${label}':\n${summarizeCapabilities(this.lastProbe.probe.caps).join("\n")}`;
      return;
    }
    this.bar.tooltip = selected
      ? `MicroPython debug target: ${selected}\n${UNPROBED_TEXT}`
      : `MicroPython debug target: none selected (click to choose)\n${UNPROBED_TEXT}`;
  }

  dispose(): void {
    for (const d of this.disposables) {
      d.dispose();
    }
  }
}
