import assert from "node:assert/strict";
import { test } from "node:test";

import { buildAttachConfig, summarizeCapabilities } from "../config";
import { Handshake } from "../handshake";

const handshake: Handshake = {
  host: "192.168.1.42",
  port: 5678,
  caps: { settrace: true, save_names: true, set_local: false, f_back: true },
};

test("buildAttachConfig produces a debugpy attach config with absolute pathMappings", () => {
  const config = buildAttachConfig({
    handshake,
    localRoot: "/home/dev/project-a/src",
    remoteRoot: "/home/dev/project-a/src",
  });
  assert.equal(config.type, "debugpy");
  assert.equal(config.request, "attach");
  assert.equal(config.justMyCode, false);
  assert.deepEqual(config.connect, { host: "192.168.1.42", port: 5678 });
  assert.deepEqual(config.pathMappings, [
    { localRoot: "/home/dev/project-a/src", remoteRoot: "/home/dev/project-a/src" },
  ]);
});

test("buildAttachConfig keeps distinct absolute roots for a multi-root-style folder", () => {
  // A workspace folder that is not the process cwd: local and remote must
  // stay independently absolute, never relative to some other root.
  const config = buildAttachConfig({
    handshake,
    localRoot: "/home/dev/workspace/other-folder",
    remoteRoot: "/home/dev/workspace/other-folder/device-copy",
  });
  assert.deepEqual(config.pathMappings, [
    {
      localRoot: "/home/dev/workspace/other-folder",
      remoteRoot: "/home/dev/workspace/other-folder/device-copy",
    },
  ]);
});

test("buildAttachConfig omits pathMappings when local/remote roots are unknown", () => {
  const config = buildAttachConfig({ handshake });
  assert.equal(config.pathMappings, undefined);
});

test("buildAttachConfig defaults the session name", () => {
  const config = buildAttachConfig({ handshake, localRoot: "/a", remoteRoot: "/a" });
  assert.equal(config.name, "MicroPython: Attach");
});

test("buildAttachConfig uses a caller-supplied name", () => {
  const config = buildAttachConfig({
    handshake,
    name: "MicroPython: pico",
    localRoot: "/a",
    remoteRoot: "/a",
  });
  assert.equal(config.name, "MicroPython: pico");
});

test("summarizeCapabilities never claims editable locals when set_local is false", () => {
  const notes = summarizeCapabilities({ set_local: false });
  assert.ok(notes.some((n) => /read-only/i.test(n)));
  assert.ok(!notes.some((n) => /editable/i.test(n)));
});

test("summarizeCapabilities reports editable locals when set_local is true", () => {
  const notes = summarizeCapabilities({ set_local: true });
  assert.ok(notes.some((n) => /editable/i.test(n)));
});
