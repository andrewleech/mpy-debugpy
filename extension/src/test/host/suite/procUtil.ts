/**
 * Process-table helpers for asserting on the real `mpremote debug` child
 * rather than a mock, a bounded poll for waiting on debug-API state without
 * a fixed sleep, and a spy for the error toasts the resolver raises (there
 * is no public API to read them back otherwise).
 */

import * as fs from "node:fs";
import * as vscode from "vscode";

/** Ppid of a `/proc` entry, or undefined if it can't be read (already gone). */
function parentPid(entry: string): number | undefined {
  let stat: string;
  try {
    stat = fs.readFileSync(`/proc/${entry}/stat`, "utf8");
  } catch {
    return undefined;
  }
  // comm (field 2) is parenthesised and may itself contain ")"; ppid (field
  // 4) is the second field after the last ")".
  const afterComm = stat.slice(stat.lastIndexOf(")") + 2);
  return Number(afterComm.split(" ")[1]);
}

/**
 * PIDs of live direct children of this process (the extension host, since
 * that's what runs this suite) whose argv names an `mpremote ... debug`
 * run. Scoped to direct children, not the whole process table: an unrelated
 * `mpremote ... debug` from another tool (e.g. a concurrent pytest run)
 * would otherwise be indistinguishable from the child this suite spawned.
 *
 * Linux-only (`/proc`); callers must check `process.platform` themselves.
 */
export function listMpremotePids(): number[] {
  const pids: number[] = [];
  for (const entry of fs.readdirSync("/proc")) {
    if (!/^\d+$/.test(entry) || parentPid(entry) !== process.pid) {
      continue;
    }
    let args: string[];
    try {
      args = fs.readFileSync(`/proc/${entry}/cmdline`, "utf8").split("\0").filter(Boolean);
    } catch {
      continue; // process exited between readdir and read
    }
    if (args.some((a) => a.includes("mpremote")) && args.includes("debug")) {
      pids.push(Number(entry));
    }
  }
  return pids;
}

/** Throws with an explicit message if `listMpremotePids` can't run here. */
export function requireProcTable(): void {
  if (process.platform !== "linux") {
    throw new Error(
      `listMpremotePids() reads /proc and only works on Linux (got ${process.platform}); ` +
        "the child-tracking tests need a different process lister on other platforms."
    );
  }
}

export function pidAlive(pid: number): boolean {
  try {
    process.kill(pid, 0);
    return true;
  } catch {
    return false;
  }
}

/** Polls `predicate` until true or `timeoutMs` elapses; never a fixed sleep. */
export async function waitFor(predicate: () => boolean, timeoutMs: number, intervalMs = 50): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (!predicate()) {
    if (Date.now() >= deadline) {
      throw new Error(`condition not met within ${timeoutMs}ms`);
    }
    await new Promise((resolve) => setTimeout(resolve, intervalMs));
  }
}

/**
 * Replaces `vscode.window.showErrorMessage` with one that records every
 * message shown, for the duration of `fn`, then restores it - the only way
 * to observe the resolver's error toasts, since `OutputChannel` content
 * isn't readable through the public API either.
 */
export async function withErrorMessageSpy<T>(fn: (errors: string[]) => Promise<T>): Promise<T> {
  const errors: string[] = [];
  const original = vscode.window.showErrorMessage;
  (vscode.window as { showErrorMessage: typeof vscode.window.showErrorMessage }).showErrorMessage = ((
    message: string
  ) => {
    errors.push(message);
    return Promise.resolve(undefined);
  }) as unknown as typeof vscode.window.showErrorMessage;
  try {
    return await fn(errors);
  } finally {
    (vscode.window as { showErrorMessage: typeof vscode.window.showErrorMessage }).showErrorMessage = original;
  }
}

/** Same pattern as `withErrorMessageSpy`, for `showInformationMessage`. */
export async function withInformationMessageSpy<T>(fn: (messages: string[]) => Promise<T>): Promise<T> {
  const messages: string[] = [];
  const original = vscode.window.showInformationMessage;
  (vscode.window as { showInformationMessage: typeof vscode.window.showInformationMessage }).showInformationMessage =
    ((message: string) => {
      messages.push(message);
      return Promise.resolve(undefined);
    }) as unknown as typeof vscode.window.showInformationMessage;
  try {
    return await fn(messages);
  } finally {
    (
      vscode.window as { showInformationMessage: typeof vscode.window.showInformationMessage }
    ).showInformationMessage = original;
  }
}

/**
 * Replaces `vscode.window.showQuickPick` for the duration of `fn` with one
 * that resolves the items it's shown (skipping VS Code's own picker UI
 * entirely) to whichever item's `label` matches `pickLabel`, or `undefined`
 * if none does - the same "user cancelled" result a real QuickPick gives.
 */
export async function withQuickPickStub<T>(pickLabel: string, fn: () => Promise<T>): Promise<T> {
  const original = vscode.window.showQuickPick;
  (vscode.window as { showQuickPick: typeof vscode.window.showQuickPick }).showQuickPick = (async (
    itemsOrPromise: readonly vscode.QuickPickItem[] | Thenable<readonly vscode.QuickPickItem[]>
  ) => {
    const items = await itemsOrPromise;
    return items.find((i) => i.label === pickLabel);
  }) as unknown as typeof vscode.window.showQuickPick;
  try {
    return await fn();
  } finally {
    (vscode.window as { showQuickPick: typeof vscode.window.showQuickPick }).showQuickPick = original;
  }
}
