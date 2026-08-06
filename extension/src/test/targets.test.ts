import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { test } from "node:test";

import { CONFIG_FILENAME, Target, discoverTargets, findConfigFile, loadTargetsFromFile } from "../targets";

/** A fixture tree the test owns and cleans up, even on failure. */
function withTempTree(fn: (root: string) => void): void {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "mpy-debugpy-targets-"));
  try {
    fn(root);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
}

function write(root: string, ...segments: [...string[], string]): void {
  const rel = segments.slice(0, -1) as string[];
  const content = segments[segments.length - 1];
  const dir = path.join(root, ...rel.slice(0, -1));
  fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(path.join(dir, rel[rel.length - 1]), content);
}

function mkdirp(root: string, ...rel: string[]): string {
  const dir = path.join(root, ...rel);
  fs.mkdirSync(dir, { recursive: true });
  return dir;
}

// --- findConfigFile: the discovery walk ---

test("findConfigFile finds a file in a parent directory", () => {
  withTempTree((root) => {
    write(root, CONFIG_FILENAME, '[target.unix]\nkind = "unix"\n');
    const start = mkdirp(root, "a", "b", "c");
    assert.equal(findConfigFile(start), path.join(root, CONFIG_FILENAME));
  });
});

test("findConfigFile finds a file in the start directory itself", () => {
  withTempTree((root) => {
    write(root, "proj", CONFIG_FILENAME, '[target.unix]\nkind = "unix"\n');
    assert.equal(findConfigFile(path.join(root, "proj")), path.join(root, "proj", CONFIG_FILENAME));
  });
});

test("findConfigFile stops at a .git directory barrier", () => {
  withTempTree((root) => {
    write(root, CONFIG_FILENAME, '[target.unix]\nkind = "unix"\n');
    mkdirp(root, "a", ".git");
    const start = mkdirp(root, "a", "b");
    assert.equal(findConfigFile(start), undefined);
  });
});

test("findConfigFile stops at a .git file barrier (worktree/submodule 'gitdir:' pointer)", () => {
  withTempTree((root) => {
    write(root, CONFIG_FILENAME, '[target.unix]\nkind = "unix"\n');
    write(root, "a", ".git", "gitdir: /elsewhere/.git/worktrees/foo\n");
    const start = mkdirp(root, "a", "b");
    assert.equal(findConfigFile(start), undefined);
  });
});

test("findConfigFile returns undefined when nothing is found", () => {
  withTempTree((root) => {
    const start = mkdirp(root, "a", "b", "c");
    assert.equal(findConfigFile(start), undefined);
  });
});

test("findConfigFile stops above $HOME", () => {
  withTempTree((root) => {
    write(root, CONFIG_FILENAME, '[target.unix]\nkind = "unix"\n');
    const home = mkdirp(root, "home", "someone");
    const start = mkdirp(root, "home", "someone", "project");
    const originalHome = process.env.HOME;
    process.env.HOME = home;
    try {
      assert.equal(findConfigFile(start), undefined);
    } finally {
      process.env.HOME = originalHome;
    }
  });
});

// --- loadTargetsFromFile / discoverTargets: parsing and validation ---

function targetsOf(root: string, relFile: string): Target[] {
  const result = loadTargetsFromFile(path.join(root, relFile));
  assert.equal(result.ok, true, result.ok ? "" : result.error.message);
  return result.ok ? result.targets : [];
}

test("parses a flat target table with all optional fields", () => {
  withTempTree((root) => {
    write(
      root,
      CONFIG_FILENAME,
      [
        "[target.pico]",
        'kind = "serial"',
        'device = "/dev/serial/by-id/usb-MicroPython_..."',
        'firmware = "fw-abc"',
        'program = "app:main"',
        'requires = ["settrace", "save_names"]',
      ].join("\n")
    );
    const targets = targetsOf(root, CONFIG_FILENAME);
    assert.deepEqual(targets, [
      {
        name: "pico",
        kind: "serial",
        device: "/dev/serial/by-id/usb-MicroPython_...",
        firmware: "fw-abc",
        program: "app:main",
        requires: ["settrace", "save_names"],
      },
    ]);
  });
});

test("parses a target with only the required 'kind'", () => {
  withTempTree((root) => {
    write(root, CONFIG_FILENAME, '[target.unix]\nkind = "unix"\n');
    assert.deepEqual(targetsOf(root, CONFIG_FILENAME), [{ name: "unix", kind: "unix" }]);
  });
});

test("parses several targets, preserving each independently", () => {
  withTempTree((root) => {
    write(
      root,
      CONFIG_FILENAME,
      ['[target.unix]', 'kind = "unix"', "", "[target.net]", 'kind = "network"', 'device = "esp32"'].join("\n")
    );
    const targets = targetsOf(root, CONFIG_FILENAME);
    assert.equal(targets.length, 2);
    assert.deepEqual(
      targets.find((t) => t.name === "unix"),
      { name: "unix", kind: "unix" }
    );
    assert.deepEqual(
      targets.find((t) => t.name === "net"),
      { name: "net", kind: "network", device: "esp32" }
    );
  });
});

test("unknown keys in a target table are ignored, not rejected", () => {
  withTempTree((root) => {
    write(root, CONFIG_FILENAME, '[target.unix]\nkind = "unix"\nicon = "rocket"\norder = 1\n');
    assert.deepEqual(targetsOf(root, CONFIG_FILENAME), [{ name: "unix", kind: "unix" }]);
  });
});

test("no [target.*] entries at all yields an empty, still-ok target list", () => {
  withTempTree((root) => {
    write(root, CONFIG_FILENAME, "# nothing here\n");
    assert.deepEqual(targetsOf(root, CONFIG_FILENAME), []);
  });
});

// --- error shapes: each must be actionable, never a raw parser throw ---

test("missing config file: 'no-config' error via discoverTargets", () => {
  withTempTree((root) => {
    const start = mkdirp(root, "a", "b");
    const result = discoverTargets(start);
    assert.equal(result.ok, false);
    if (!result.ok) {
      assert.equal(result.error.kind, "no-config");
      assert.match(result.error.message, /no mpdebug\.toml found/);
    }
  });
});

test("malformed TOML: 'parse-error', not a raw parser throw", () => {
  withTempTree((root) => {
    write(root, CONFIG_FILENAME, "[target.unix\nkind = unix\n");
    assert.doesNotThrow(() => loadTargetsFromFile(path.join(root, CONFIG_FILENAME)));
    const result = loadTargetsFromFile(path.join(root, CONFIG_FILENAME));
    assert.equal(result.ok, false);
    if (!result.ok) {
      assert.equal(result.error.kind, "parse-error");
    }
  });
});

test("'target' is not a table: 'invalid-target'", () => {
  withTempTree((root) => {
    write(root, CONFIG_FILENAME, 'target = "not a table"\n');
    const result = loadTargetsFromFile(path.join(root, CONFIG_FILENAME));
    assert.equal(result.ok, false);
    if (!result.ok) {
      assert.equal(result.error.kind, "invalid-target");
    }
  });
});

test("a target entry that is not a table: 'invalid-target'", () => {
  withTempTree((root) => {
    write(root, CONFIG_FILENAME, 'target.unix = "oops"\n');
    const result = loadTargetsFromFile(path.join(root, CONFIG_FILENAME));
    assert.equal(result.ok, false);
    if (!result.ok) {
      assert.equal(result.error.kind, "invalid-target");
    }
  });
});

test("missing 'kind': 'invalid-target'", () => {
  withTempTree((root) => {
    write(root, CONFIG_FILENAME, "[target.unix]\n");
    const result = loadTargetsFromFile(path.join(root, CONFIG_FILENAME));
    assert.equal(result.ok, false);
    if (!result.ok) {
      assert.equal(result.error.kind, "invalid-target");
      assert.match(result.error.message, /kind/);
    }
  });
});

test("invalid 'kind' value: 'invalid-target'", () => {
  withTempTree((root) => {
    write(root, CONFIG_FILENAME, '[target.unix]\nkind = "bluetooth"\n');
    const result = loadTargetsFromFile(path.join(root, CONFIG_FILENAME));
    assert.equal(result.ok, false);
    if (!result.ok) {
      assert.match(result.error.message, /bluetooth/);
    }
  });
});

test("a nested table under a target with no 'kind' names the nesting", () => {
  withTempTree((root) => {
    write(root, CONFIG_FILENAME, "[target.unix.sub]\nkind = \"unix\"\n");
    const result = loadTargetsFromFile(path.join(root, CONFIG_FILENAME));
    assert.equal(result.ok, false);
    if (!result.ok) {
      assert.match(result.error.message, /nested table/);
    }
  });
});

test("'requires' with an unknown capability: 'invalid-target'", () => {
  withTempTree((root) => {
    write(root, CONFIG_FILENAME, '[target.unix]\nkind = "unix"\nrequires = ["settrace", "telepathy"]\n');
    const result = loadTargetsFromFile(path.join(root, CONFIG_FILENAME));
    assert.equal(result.ok, false);
    if (!result.ok) {
      assert.equal(result.error.kind, "invalid-target");
      assert.match(result.error.message, /telepathy/);
    }
  });
});

test("'requires' that is not a list of strings: 'invalid-target'", () => {
  withTempTree((root) => {
    write(root, CONFIG_FILENAME, '[target.unix]\nkind = "unix"\nrequires = [1, 2]\n');
    const result = loadTargetsFromFile(path.join(root, CONFIG_FILENAME));
    assert.equal(result.ok, false);
    if (!result.ok) {
      assert.equal(result.error.kind, "invalid-target");
    }
  });
});

test("kind 'serial' with no 'device': 'invalid-target'", () => {
  withTempTree((root) => {
    write(root, CONFIG_FILENAME, '[target.pico]\nkind = "serial"\n');
    const result = loadTargetsFromFile(path.join(root, CONFIG_FILENAME));
    assert.equal(result.ok, false);
    if (!result.ok) {
      assert.match(result.error.message, /'device'/);
    }
  });
});

test("an empty 'device' string: 'invalid-target'", () => {
  withTempTree((root) => {
    write(root, CONFIG_FILENAME, '[target.net]\nkind = "network"\ndevice = ""\n');
    const result = loadTargetsFromFile(path.join(root, CONFIG_FILENAME));
    assert.equal(result.ok, false);
    if (!result.ok) {
      assert.match(result.error.message, /empty/);
    }
  });
});

test("'device' of the wrong type: 'invalid-target'", () => {
  withTempTree((root) => {
    write(root, CONFIG_FILENAME, "[target.net]\nkind = \"network\"\ndevice = 42\n");
    const result = loadTargetsFromFile(path.join(root, CONFIG_FILENAME));
    assert.equal(result.ok, false);
    if (!result.ok) {
      assert.equal(result.error.kind, "invalid-target");
    }
  });
});

test("an unreadable config file: 'read-error', not a raw throw", () => {
  if (process.getuid && process.getuid() === 0) {
    // root ignores file permissions, so the fixture would still be
    // readable and the assertion below would fail for an unrelated reason.
    return;
  }
  withTempTree((root) => {
    write(root, CONFIG_FILENAME, '[target.unix]\nkind = "unix"\n');
    const configPath = path.join(root, CONFIG_FILENAME);
    fs.chmodSync(configPath, 0o000);
    try {
      const result = loadTargetsFromFile(configPath);
      assert.equal(result.ok, false);
      if (!result.ok) {
        assert.equal(result.error.kind, "read-error");
        assert.equal(result.error.message.includes(configPath), true);
      }
    } finally {
      fs.chmodSync(configPath, 0o644);
    }
  });
});

// smol-toml rejects integers outside the JS-safe/64-bit range, where
// Python's tomllib accepts them (see targets.ts's module header). Only an
// unknown, discarded key could realistically carry one; pinned here so a
// smol-toml upgrade that changes this is caught rather than assumed fixed.
test("an out-of-range integer in an unknown key: 'parse-error' (known smol-toml limitation)", () => {
  withTempTree((root) => {
    write(root, CONFIG_FILENAME, '[target.pico]\nkind = "unix"\nserial = 0xDEADBEEFDEADBEEF\n');
    const result = loadTargetsFromFile(path.join(root, CONFIG_FILENAME));
    assert.equal(result.ok, false);
    if (!result.ok) {
      assert.equal(result.error.kind, "parse-error");
    }
  });
});

// --- adversarial: capability display comes from the probe, never the name ---

test("a target literally named after a capability carries no implied capability", () => {
  withTempTree((root) => {
    write(root, CONFIG_FILENAME, '[target.set_local]\nkind = "unix"\n');
    const targets = targetsOf(root, CONFIG_FILENAME);
    assert.deepEqual(targets, [{ name: "set_local", kind: "unix" }]);
    // The parsed record for this target carries nothing that looks like a
    // capability flag; only `requires` (a request, not a probed truth) can
    // ever appear, and it is absent here.
    assert.equal("requires" in targets[0], false);
  });
});
