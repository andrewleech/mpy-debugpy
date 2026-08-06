/**
 * Reads named debug targets from a project's `mpdebug.toml`.
 *
 * Pure: no `vscode` import, so it is unit-testable and cross-checkable
 * against the Python implementation without a VS Code host.
 *
 * The schema and discovery walk are owned by
 * `micropython/tools/mpremote/mpremote/mpdebug_config.py` - this module
 * mirrors both rather than shelling out to it, because the command exposes
 * no machine-readable target list. Keep the two in step by hand; the
 * cross-check test in `src/test/targetsCrossCheck.test.ts` runs both
 * implementations over the same fixture trees and asserts they agree.
 *
 * Known divergence: `smol-toml` rejects integers outside the JS
 * safe-integer/64-bit range, where Python's `tomllib` accepts them.
 * Unaffected in practice since only unknown (discarded) keys could carry
 * such a value - see the pinning test in `src/test/targets.test.ts`.
 */

import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";

import { parse as parseToml } from "smol-toml";

export const CONFIG_FILENAME = "mpdebug.toml";

/** Mirrors `mpdebug_config.KNOWN_CAPABILITIES`. */
export const KNOWN_CAPABILITIES = ["settrace", "save_names", "set_local", "f_back"] as const;
export type Capability = (typeof KNOWN_CAPABILITIES)[number];

const KINDS = ["unix", "serial", "network"] as const;
export type TargetKind = (typeof KINDS)[number];

export interface Target {
  name: string;
  kind: TargetKind;
  device?: string;
  firmware?: string;
  program?: string;
  requires?: Capability[];
}

export type TargetsError =
  | { kind: "no-config"; message: string }
  | { kind: "read-error"; message: string }
  | { kind: "parse-error"; message: string }
  | { kind: "invalid-target"; message: string };

export type TargetsResult =
  | { ok: true; path: string; targets: Target[] }
  | { ok: false; error: TargetsError };

/**
 * Walks from `startDir` (default cwd) upward for `mpdebug.toml`; mirrors
 * `mpdebug_config.find_config`. Stops above the home directory and at any
 * directory holding a `.git` entry (directory or `gitdir:` file - only
 * existence is checked, matching the Python side), so a config outside the
 * project root or above `$HOME` is never picked up.
 */
export function findConfigFile(startDir?: string): string | undefined {
  let dir = path.resolve(startDir ?? process.cwd());
  const home = path.resolve(os.homedir());
  for (;;) {
    const candidate = path.join(dir, CONFIG_FILENAME);
    if (isFile(candidate)) {
      return candidate;
    }
    if (fs.existsSync(path.join(dir, ".git")) || dir === home) {
      return undefined;
    }
    const parent = path.dirname(dir);
    if (parent === dir) {
      return undefined;
    }
    dir = parent;
  }
}

function isFile(p: string): boolean {
  try {
    return fs.statSync(p).isFile();
  } catch {
    return false;
  }
}

/** Discovers and parses the project's targets; never throws. */
export function discoverTargets(startDir?: string): TargetsResult {
  const configPath = findConfigFile(startDir);
  if (configPath === undefined) {
    return {
      ok: false,
      error: {
        kind: "no-config",
        message:
          `no ${CONFIG_FILENAME} found above ${path.resolve(startDir ?? process.cwd())}; ` +
          "add a [target.<name>] table to enable the target picker, or set an explicit " +
          '"target" in launch.json.',
      },
    };
  }
  return loadTargetsFromFile(configPath);
}

/** Parses one `mpdebug.toml` file at `configPath`; never throws. */
export function loadTargetsFromFile(configPath: string): TargetsResult {
  let text: string;
  try {
    text = fs.readFileSync(configPath, "utf8");
  } catch (err) {
    return { ok: false, error: { kind: "read-error", message: `${configPath}: ${(err as Error).message}` } };
  }

  let data: unknown;
  try {
    data = parseToml(text);
  } catch (err) {
    return {
      ok: false,
      error: { kind: "parse-error", message: `${configPath}: invalid TOML: ${(err as Error).message}` },
    };
  }

  return parseTargetsData(data, configPath);
}

function invalidTarget(configPath: string, message: string): TargetsResult {
  return { ok: false, error: { kind: "invalid-target", message: `${configPath}: ${message}` } };
}

/** Mirrors `mpdebug_config._load_targets`'s validation, error for error. */
function parseTargetsData(data: unknown, configPath: string): TargetsResult {
  const root = data as Record<string, unknown>;
  const raw = root.target ?? {};
  if (typeof raw !== "object" || raw === null || Array.isArray(raw)) {
    return invalidTarget(configPath, "'target' must be a table of [target.<name>] entries");
  }

  const targets: Target[] = [];
  for (const [name, rawSpec] of Object.entries(raw as Record<string, unknown>)) {
    if (typeof rawSpec !== "object" || rawSpec === null || Array.isArray(rawSpec)) {
      return invalidTarget(configPath, `target '${name}' must be a table`);
    }
    const spec = rawSpec as Record<string, unknown>;

    const kind = spec.kind;
    if (typeof kind !== "string" || !(KINDS as readonly string[]).includes(kind)) {
      if (kind === undefined && Object.values(spec).some((v) => typeof v === "object" && v !== null && !Array.isArray(v))) {
        return invalidTarget(
          configPath,
          `target '${name}' has a nested table (e.g. [target.${name}.<sub>]); a target entry must be a flat table`
        );
      }
      return invalidTarget(
        configPath,
        `target '${name}' has kind ${JSON.stringify(kind)}, expected one of ${KINDS.join(", ")}`
      );
    }

    const requiresRaw = spec.requires ?? [];
    if (!Array.isArray(requiresRaw) || !requiresRaw.every((r) => typeof r === "string")) {
      return invalidTarget(configPath, `target '${name}' requires must be a list of capability names`);
    }
    const unknown = requiresRaw.filter((r) => !(KNOWN_CAPABILITIES as readonly string[]).includes(r));
    if (unknown.length > 0) {
      return invalidTarget(
        configPath,
        `target '${name}' requires unknown capability ${unknown.map((u) => `'${u}'`).join(", ")}; ` +
          `the probe only reports ${KNOWN_CAPABILITIES.join(", ")}`
      );
    }

    for (const key of ["device", "program", "firmware"] as const) {
      const value = spec[key];
      if (value !== undefined && typeof value !== "string") {
        return invalidTarget(configPath, `target '${name}' ${key} must be a string`);
      }
    }

    const device = spec.device as string | undefined;
    if (kind === "serial" && !device) {
      return invalidTarget(configPath, `target '${name}' is kind 'serial' but has no 'device'`);
    }
    if (device === "") {
      return invalidTarget(configPath, `target '${name}' has an empty 'device'`);
    }

    targets.push({
      name,
      kind: kind as TargetKind,
      ...(device !== undefined ? { device } : {}),
      ...(spec.firmware !== undefined ? { firmware: spec.firmware as string } : {}),
      ...(spec.program !== undefined ? { program: spec.program as string } : {}),
      ...(requiresRaw.length > 0 ? { requires: requiresRaw as Capability[] } : {}),
    });
  }

  return { ok: true, path: configPath, targets };
}
