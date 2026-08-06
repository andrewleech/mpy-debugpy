/**
 * Cross-checks `findConfigFile` against `mpdebug_config.find_config`, and
 * `loadTargetsFromFile` against `mpdebug_config._load_targets`, over the
 * same fixture trees - independent implementations of the same walk and
 * validation that can drift without either side's own tests noticing.
 *
 * Skipped if python3 or the mpremote submodule tree is unavailable.
 */

import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { test } from "node:test";

import { CONFIG_FILENAME, Target, findConfigFile, loadTargetsFromFile } from "../targets";

const REPO = path.resolve(__dirname, "..", "..", "..");
const MPREMOTE_DIR = path.join(REPO, "micropython", "tools", "mpremote");

const PYTHON_FIND_CONFIG_SCRIPT = [
  "import json, sys",
  "sys.path.insert(0, sys.argv[1])",
  "from mpremote import mpdebug_config",
  "print(json.dumps(mpdebug_config.find_config(sys.argv[2])))",
].join("\n");

// Serialises `_load_targets`'s result to the same shape `loadTargetsFromFile`
// produces on success ({name, kind, device?, firmware?, program?, requires?}
// per target), or {error: <CommandError message>} on failure - so the two
// sides can be compared field-for-field rather than just pass/fail.
const PYTHON_LOAD_TARGETS_SCRIPT = [
  "import json, sys",
  "sys.path.insert(0, sys.argv[1])",
  "from mpremote import mpdebug_config",
  "from mpremote.commands import CommandError",
  "try:",
  "    targets = mpdebug_config._load_targets(sys.argv[2])",
  "    out = []",
  "    for t in targets.values():",
  "        entry = {'name': t.name, 'kind': t.kind}",
  "        if t.device is not None: entry['device'] = t.device",
  "        if t.firmware is not None: entry['firmware'] = t.firmware",
  "        if t.program is not None: entry['program'] = t.program",
  "        if t.requires: entry['requires'] = list(t.requires)",
  "        out.append(entry)",
  "    print(json.dumps({'targets': sorted(out, key=lambda e: e['name'])}))",
  "except CommandError as er:",
  "    print(json.dumps({'error': str(er)}))",
].join("\n");

function pythonAvailable(): boolean {
  if (!fs.existsSync(path.join(MPREMOTE_DIR, "mpremote", "mpdebug_config.py"))) {
    return false;
  }
  try {
    execFileSync("python3", ["-c", PYTHON_FIND_CONFIG_SCRIPT, MPREMOTE_DIR, os.tmpdir()], { stdio: "pipe" });
    return true;
  } catch {
    return false;
  }
}

/** Runs the real `mpdebug_config._load_targets(path)`, normalised for comparison. */
function pythonLoadTargets(configPath: string): { targets: Target[] } | { error: string } {
  const out = execFileSync("python3", ["-c", PYTHON_LOAD_TARGETS_SCRIPT, MPREMOTE_DIR, configPath], {
    encoding: "utf8",
  });
  return JSON.parse(out.trim());
}

function tsLoadTargets(configPath: string): { targets: Target[] } | { error: string } {
  const result = loadTargetsFromFile(configPath);
  if (!result.ok) {
    return { error: result.error.message };
  }
  return { targets: [...result.targets].sort((a, b) => a.name.localeCompare(b.name)) };
}

/** Asserts agreement on outcome (ok/error) and, when ok, on the parsed targets. */
function assertLoadTargetsAgree(configPath: string): void {
  const py = pythonLoadTargets(configPath);
  const ts = tsLoadTargets(configPath);
  assert.equal("error" in ts, "error" in py, `TS (${JSON.stringify(ts)}) and Python (${JSON.stringify(py)}) disagree on success for ${configPath}`);
  if (!("error" in py) && !("error" in ts)) {
    assert.deepEqual(ts.targets, py.targets, `TS and Python parsed different targets for ${configPath}`);
  }
}

/** Runs the real `mpdebug_config.find_config(startDir)`; null if none. */
function pythonFindConfig(startDir: string, homeEnv?: string): string | null {
  const out = execFileSync("python3", ["-c", PYTHON_FIND_CONFIG_SCRIPT, MPREMOTE_DIR, startDir], {
    encoding: "utf8",
    env: { ...process.env, ...(homeEnv !== undefined ? { HOME: homeEnv } : {}) },
  });
  return JSON.parse(out.trim());
}

function tsFindConfig(startDir: string, homeEnv?: string): string | undefined {
  if (homeEnv === undefined) {
    return findConfigFile(startDir);
  }
  const original = process.env.HOME;
  process.env.HOME = homeEnv;
  try {
    return findConfigFile(startDir);
  } finally {
    process.env.HOME = original;
  }
}

function assertAgree(startDir: string, homeEnv?: string): void {
  const py = pythonFindConfig(startDir, homeEnv);
  const ts = tsFindConfig(startDir, homeEnv);
  assert.equal(ts ?? null, py, `TS (${ts}) and Python (${py}) disagree for ${startDir}`);
}

const available = pythonAvailable();

function withTempTree(fn: (root: string) => void): void {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "mpy-debugpy-crosscheck-"));
  try {
    fn(root);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
}

function mkdirp(root: string, ...rel: string[]): string {
  const dir = path.join(root, ...rel);
  fs.mkdirSync(dir, { recursive: true });
  return dir;
}

function write(dir: string, name: string, content: string): void {
  fs.writeFileSync(path.join(dir, name), content);
}

test(
  "TS and Python agree: config found several directories up",
  { skip: !available && "python3 or the mpremote submodule is unavailable" },
  () => {
    withTempTree((root) => {
      write(root, CONFIG_FILENAME, '[target.unix]\nkind = "unix"\n');
      assertAgree(mkdirp(root, "a", "b", "c"));
    });
  }
);

test(
  "TS and Python agree: a .git directory stops the walk",
  { skip: !available && "python3 or the mpremote submodule is unavailable" },
  () => {
    withTempTree((root) => {
      write(root, CONFIG_FILENAME, '[target.unix]\nkind = "unix"\n');
      mkdirp(root, "a", ".git");
      assertAgree(mkdirp(root, "a", "b"));
    });
  }
);

test(
  "TS and Python agree: a .git 'gitdir:' file stops the walk",
  { skip: !available && "python3 or the mpremote submodule is unavailable" },
  () => {
    withTempTree((root) => {
      write(root, CONFIG_FILENAME, '[target.unix]\nkind = "unix"\n');
      const a = mkdirp(root, "a");
      write(a, ".git", "gitdir: /elsewhere/.git/worktrees/foo\n");
      assertAgree(mkdirp(root, "a", "b"));
    });
  }
);

test(
  "TS and Python agree: nothing found anywhere",
  { skip: !available && "python3 or the mpremote submodule is unavailable" },
  () => {
    withTempTree((root) => {
      assertAgree(mkdirp(root, "a", "b", "c"));
    });
  }
);

test(
  "TS and Python agree: the walk stops above $HOME",
  { skip: !available && "python3 or the mpremote submodule is unavailable" },
  () => {
    withTempTree((root) => {
      write(root, CONFIG_FILENAME, '[target.unix]\nkind = "unix"\n');
      const home = mkdirp(root, "home", "someone");
      const start = mkdirp(root, "home", "someone", "project");
      assertAgree(start, home);
    });
  }
);

// --- _load_targets / loadTargetsFromFile: validation, not just discovery ---

test(
  "TS and Python agree: a valid multi-target file",
  { skip: !available && "python3 or the mpremote submodule is unavailable" },
  () => {
    withTempTree((root) => {
      const configPath = path.join(root, CONFIG_FILENAME);
      write(
        root,
        CONFIG_FILENAME,
        [
          "[target.unix]",
          'kind = "unix"',
          "",
          "[target.pico]",
          'kind = "serial"',
          'device = "/dev/serial/by-id/usb-MicroPython_..."',
          'requires = ["settrace", "save_names"]',
        ].join("\n")
      );
      assertLoadTargetsAgree(configPath);
    });
  }
);

test(
  "TS and Python agree: an unknown kind is rejected",
  { skip: !available && "python3 or the mpremote submodule is unavailable" },
  () => {
    withTempTree((root) => {
      const configPath = path.join(root, CONFIG_FILENAME);
      write(root, CONFIG_FILENAME, '[target.unix]\nkind = "bluetooth"\n');
      assertLoadTargetsAgree(configPath);
    });
  }
);

test(
  "TS and Python agree: an unknown 'requires' capability is rejected",
  { skip: !available && "python3 or the mpremote submodule is unavailable" },
  () => {
    withTempTree((root) => {
      const configPath = path.join(root, CONFIG_FILENAME);
      write(root, CONFIG_FILENAME, '[target.unix]\nkind = "unix"\nrequires = ["telepathy"]\n');
      assertLoadTargetsAgree(configPath);
    });
  }
);

test(
  "TS and Python agree: a serial target with no device is rejected",
  { skip: !available && "python3 or the mpremote submodule is unavailable" },
  () => {
    withTempTree((root) => {
      const configPath = path.join(root, CONFIG_FILENAME);
      write(root, CONFIG_FILENAME, '[target.pico]\nkind = "serial"\n');
      assertLoadTargetsAgree(configPath);
    });
  }
);
