/**
 * Reads the repo's checked-in `.vscode/launch.json` for tests that drive it
 * for real (the integration suite and the extension-host suite both need
 * the exact config a user's F5 gets, not a copy that can drift from it).
 */

import * as fs from "node:fs";
import * as path from "node:path";

/** Strips `.vscode/launch.json`'s whole-line `//` comments and parses it. */
export function readLaunchConfig(repoRoot: string): Record<string, unknown> {
  const raw = fs.readFileSync(path.join(repoRoot, ".vscode", "launch.json"), "utf8");
  const stripped = raw
    .split("\n")
    .filter((line) => !line.trimStart().startsWith("//"))
    .join("\n");
  return JSON.parse(stripped).configurations[0];
}
