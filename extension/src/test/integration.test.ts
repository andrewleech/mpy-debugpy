/**
 * Drives the real `mpremote debug unix` command through `runDebugCommand`.
 *
 * The other suites feed a fake child, which proves the parsing and lifetime
 * logic but not that the two sides still agree on the wire format. This one
 * spawns the actual command against the built unix firmware and asserts a
 * real handshake comes back, so a change to either side's output breaks a
 * test rather than the extension. Skipped when the firmware has not been
 * built (`make firmware-unix`).
 */

import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import path from "node:path";
import { test } from "node:test";

import { ChildProcessLike, SpawnFn, runDebugCommand } from "../command";
import { readLaunchConfig } from "./launchConfig";

const REPO = path.resolve(__dirname, "..", "..", "..");
const MPREMOTE_DIR = path.join(REPO, "micropython", "tools", "mpremote");
const FIRMWARE =
  process.env.MPY_DEBUG_FIRMWARE ??
  path.join(REPO, "micropython", "ports", "unix", "build-standard", "micropython");

const available = existsSync(FIRMWARE) && existsSync(path.join(MPREMOTE_DIR, "mpremote"));

// `python -m mpremote` from the submodule, not whatever is on PATH: the
// `debug` command lives on a feature branch and the installed mpremote may
// predate it.
const realSpawn: SpawnFn = (_command, args, options) =>
  spawn(process.env.PYTHON ?? "python3", ["-m", "mpremote", ...args], {
    cwd: options.cwd,
    env: { ...process.env, ...options.env, PYTHONPATH: MPREMOTE_DIR },
    stdio: ["ignore", "pipe", "pipe"],
  }) as unknown as ChildProcessLike;

const debugEnv = {
  MPY_DEBUG_FIRMWARE: FIRMWARE,
  MICROPYPATH: [
    path.join(REPO, "src"),
    path.join(REPO, "micropython-lib", "python-ecosys", "debugpy"),
  ].join(":"),
};

test(
  "runDebugCommand gets a real handshake from mpremote debug unix",
  { skip: available ? false : `no unix firmware at ${FIRMWARE}` },
  async () => {
    const { handshake, child } = await runDebugCommand(
      realSpawn,
      "mpremote",
      { target: "unix", program: "target:main", timeout: 30 },
      { cwd: REPO, env: debugEnv }
    );
    try {
      // The command de-wildcards the bind address before re-emitting it, so
      // what arrives here must already be connectable.
      assert.ok(handshake.host !== "0.0.0.0" && handshake.host.length > 0);
      assert.ok(Number.isInteger(handshake.port) && handshake.port > 0);
      // Probed on the device, never inferred: the firmware under test is
      // built with settrace, which is what makes it debuggable at all.
      assert.equal(handshake.caps.settrace, true);
    } finally {
      child.kill();
    }
  }
);

/**
 * The checked-in F5 config, driven for real.
 *
 * Everything in `.vscode/launch.json` except VS Code's own variable
 * substitution and `startDebugging` is exercised here, so the config cannot
 * quietly rot into one that shows an error toast instead of a session.
 */
test(
  ".vscode/launch.json's config reaches a handshake as written",
  { skip: available ? false : `no unix firmware at ${FIRMWARE}` },
  async () => {
    const config = readLaunchConfig(REPO) as {
      mpremotePath: string;
      mpremoteArgs: string[];
      target: string;
      program: string;
      cwd: string;
      env: Record<string, string>;
    };

    const expand = (value: string) => value.split("${workspaceFolder}").join(REPO);
    const env = Object.fromEntries(
      Object.entries(config.env as Record<string, string>).map(([k, v]) => [k, expand(v)])
    );

    const { handshake, child } = await runDebugCommand(
      (command, args, options) =>
        spawn(command, args, {
          cwd: options.cwd,
          env: { ...process.env, ...options.env },
          stdio: ["ignore", "pipe", "pipe"],
        }) as unknown as ChildProcessLike,
      config.mpremotePath,
      { target: config.target, program: config.program, timeout: 30 },
      { argsPrefix: config.mpremoteArgs, cwd: expand(config.cwd), env }
    );
    try {
      assert.equal(handshake.caps.settrace, true);
      assert.ok(handshake.port > 0);
    } finally {
      child.kill();
    }
  }
);

/**
 * `--loop` is only reachable from an F5 launch through this argv, and the
 * flag is the one launch-config property whose value changes what the target
 * advertises (it is what turns on `supportsRestartRequest`). A handshake
 * proves the CLI accepts what the extension now emits; the restart round trip
 * itself belongs to the extension-host suite.
 */
test(
  "the loop option reaches mpremote debug and still handshakes",
  { skip: available ? false : `no unix firmware at ${FIRMWARE}` },
  async () => {
    const { handshake, child } = await runDebugCommand(
      realSpawn,
      "mpremote",
      { target: "unix", program: "target:main", timeout: 30, loop: true },
      { cwd: REPO, env: debugEnv }
    );
    try {
      assert.equal(handshake.caps.settrace, true);
      assert.ok(handshake.port > 0);
    } finally {
      child.kill();
    }
  }
);

/**
 * The extension deliberately does not pre-check `source` against the target's
 * kind - the CLI owns that rule. This asserts the consequence: the CLI's own
 * message survives the trip back through the captured-output path, so the
 * user sees why rather than a bare exit code.
 */
test(
  "a source option on a unix target surfaces the command's own rejection",
  { skip: available ? false : "needs a working mpremote checkout" },
  async () => {
    await assert.rejects(
      runDebugCommand(
        realSpawn,
        "mpremote",
        { target: "unix", program: "target:main", timeout: 10, source: REPO },
        { cwd: REPO, env: debugEnv }
      ),
      /--source is not valid for a unix target/
    );
  }
);

test(
  "runDebugCommand reports the command's own error when the firmware is missing",
  { skip: available ? false : "needs a working mpremote checkout" },
  async () => {
    await assert.rejects(
      runDebugCommand(
        realSpawn,
        "mpremote",
        { target: "unix", program: "target:main", timeout: 10 },
        { cwd: REPO, env: { MPY_DEBUG_FIRMWARE: path.join(REPO, "no-such-binary") } }
      ),
      /no unix debug binary|failed to launch|exited/i
    );
  }
);
