/**
 * Builds the `debugpy` attach configuration from a handshake.
 *
 * Pure: no `vscode` import. The resolver in extension.ts is the only caller
 * that hands the result to `vscode.debug.startDebugging`.
 */

import { Handshake } from "./handshake";

export interface AttachConfigInput {
  handshake: Handshake;
  /** Debug-session display name; defaults to "MicroPython: Attach". */
  name?: string;
  /**
   * Absolute local directory the IDE edits, and the absolute remote
   * directory the target imports from. Omit both when the caller doesn't
   * know the two are the same directory (only true today for the unix
   * flow) - a wrong mapping is worse than none, since debugpy falls back
   * to sane defaults when `pathMappings` is absent.
   */
  localRoot?: string;
  remoteRoot?: string;
}

export interface PathMapping {
  localRoot: string;
  remoteRoot: string;
}

export interface DebugpyAttachConfig {
  type: "debugpy";
  request: "attach";
  name: string;
  connect: { host: string; port: number };
  pathMappings?: PathMapping[];
  justMyCode: boolean;
  [key: string]: unknown;
}

/**
 * `handshake.host`/`.port` are used verbatim: `mpremote debug` has already
 * resolved any wildcard bind address to a connectable one (unix: localhost;
 * device: the transport's real address), and re-mapping it here would break
 * the device transports, where the resolved address is the only connectable
 * one.
 */
export function buildAttachConfig(input: AttachConfigInput): DebugpyAttachConfig {
  const config: DebugpyAttachConfig = {
    type: "debugpy",
    request: "attach",
    name: input.name ?? "MicroPython: Attach",
    connect: { host: input.handshake.host, port: input.handshake.port },
    justMyCode: false,
  };
  if (input.localRoot !== undefined && input.remoteRoot !== undefined) {
    config.pathMappings = [{ localRoot: input.localRoot, remoteRoot: input.remoteRoot }];
  }
  return config;
}

/**
 * One line per capability that changes what the session can do, honest
 * about `set_local`: never claims local-variable editing works when the
 * firmware doesn't support it (locals arrive read-only in that case).
 */
export function summarizeCapabilities(caps: Record<string, boolean>): string[] {
  const notes: string[] = [];
  notes.push(
    caps.set_local
      ? "local variables are editable"
      : "local variables are read-only (this firmware does not support set_local)"
  );
  if (!caps.save_names) {
    notes.push("local names may show as positional placeholders (save_names not supported)");
  }
  if (!caps.f_back) {
    notes.push("caller frames are unavailable (f_back not supported)");
  }
  if (!caps.settrace) {
    notes.push("breakpoints/stepping are unavailable (settrace not supported)");
  }
  return notes;
}
