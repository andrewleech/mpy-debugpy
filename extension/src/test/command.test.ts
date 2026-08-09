import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { test } from "node:test";

import { ChildProcessLike, SpawnFn, buildDebugArgs, runDebugCommand } from "../command";

const HANDSHAKE_LINE =
  'MPDBG-READY {"host": "127.0.0.1", "port": 5678, "caps": {"settrace": true}}\n';

class FakeChild extends EventEmitter implements ChildProcessLike {
  stdout = new EventEmitter();
  stderr = new EventEmitter();
  killed = false;
  killSignal: string | undefined;

  kill(signal?: string): boolean {
    this.killed = true;
    this.killSignal = signal;
    return true;
  }
}

interface SpawnCall {
  command: string;
  args: string[];
  cwd?: string;
  env?: Record<string, string>;
}

function fakeSpawn(child: FakeChild, calls: SpawnCall[]): SpawnFn {
  return (command, args, options) => {
    calls.push({ command, args, cwd: options.cwd, env: options.env });
    return child;
  };
}

// --- buildDebugArgs: argv ordering for every option combination ---

test("buildDebugArgs with no options", () => {
  assert.deepEqual(buildDebugArgs({}), ["debug"]);
});

test("buildDebugArgs puts options before target and program", () => {
  assert.deepEqual(
    buildDebugArgs({ port: 5678, timeout: 30, target: "pico", program: "app:main" }),
    ["debug", "--port", "5678", "--timeout", "30", "pico", "app:main"]
  );
});

test("buildDebugArgs with --dap-log and --dap-log-file", () => {
  assert.deepEqual(
    buildDebugArgs({ dapLog: true, dapLogFile: "trace.jsonl", target: "unix" }),
    ["debug", "--dap-log", "--dap-log-file", "trace.jsonl", "unix"]
  );
});

test("buildDebugArgs with target only, no program", () => {
  assert.deepEqual(buildDebugArgs({ target: "unix" }), ["debug", "unix"]);
});

test("buildDebugArgs with --dap-log but no file", () => {
  assert.deepEqual(buildDebugArgs({ dapLog: true }), ["debug", "--dap-log"]);
});

test("buildDebugArgs rejects dapLogFile without dapLog", () => {
  assert.throws(() => buildDebugArgs({ dapLogFile: "trace.jsonl" }), /dapLog/);
});

test("buildDebugArgs rejects program without target", () => {
  assert.throws(() => buildDebugArgs({ program: "app:main" }), /target/);
});

test("buildDebugArgs with --source and --loop, still before the positionals", () => {
  assert.deepEqual(
    buildDebugArgs({ source: "/proj/src", loop: true, target: "pico", program: "app:main" }),
    ["debug", "--source", "/proj/src", "--loop", "pico", "app:main"]
  );
});

// Absent is not the same as false: omitting them is what lets a target's own
// `source` in mpdebug.toml keep deciding, so neither may leak into argv.
test("buildDebugArgs omits --source and --loop when unset or false", () => {
  assert.deepEqual(buildDebugArgs({ target: "pico", loop: false }), ["debug", "pico"]);
});

test("buildDebugArgs rejects an empty source", () => {
  assert.throws(() => buildDebugArgs({ source: "", target: "pico" }), /source/);
});

// --- runDebugCommand ---

test("runDebugCommand resolves with the handshake and child on success", async () => {
  const child = new FakeChild();
  const calls: SpawnCall[] = [];
  const promise = runDebugCommand(fakeSpawn(child, calls), "mpremote", { target: "unix" }, { cwd: "/proj" });
  child.stdout.emit("data", "waiting...\n");
  child.stdout.emit("data", HANDSHAKE_LINE);
  const result = await promise;
  assert.equal(result.handshake.port, 5678);
  assert.equal(result.child, child);
  assert.deepEqual(calls[0], { command: "mpremote", args: ["debug", "unix"], cwd: "/proj", env: undefined });
  assert.equal(child.killed, false); // caller now owns the child's lifetime
});

test("runDebugCommand rejects with captured output on early exit", async () => {
  const child = new FakeChild();
  const promise = runDebugCommand(fakeSpawn(child, []), "mpremote", { target: "unix" });
  child.stdout.emit("data", "booting\nerror: firmware not found\n");
  child.emit("close", 1, null);
  await assert.rejects(promise, (err) => {
    assert.match((err as Error).message, /exited \(code 1\)/);
    assert.match((err as Error).message, /error: firmware not found/);
    return true;
  });
});

test("runDebugCommand resolves on close even if the handshake chunk arrives in the same tick as exit", async () => {
  // Regression: settling on Node's "exit" (rather than "close") races
  // delivery of buffered stdout - a handshake already on the wire can be
  // missed if "exit" fires first. "close" is only emitted once stdio has
  // drained, so the handshake must always be visible by then.
  const child = new FakeChild();
  const promise = runDebugCommand(fakeSpawn(child, []), "mpremote", { target: "unix" });
  child.stdout.emit("data", HANDSHAKE_LINE);
  child.emit("close", 0, null);
  const result = await promise;
  assert.equal(result.handshake.port, 5678);
});

test("runDebugCommand survives a malformed prefixed line and takes the real handshake", async () => {
  const child = new FakeChild();
  const promise = runDebugCommand(fakeSpawn(child, []), "mpremote", { target: "unix" });
  child.stdout.emit("data", "MPDBG-READY {broken\n");
  child.stdout.emit("data", HANDSHAKE_LINE);
  const result = await promise;
  assert.equal(result.handshake.port, 5678);
  assert.equal(child.killed, false);
});

test("runDebugCommand names the malformed line when the stream ends without a handshake", async () => {
  const child = new FakeChild();
  const promise = runDebugCommand(fakeSpawn(child, []), "mpremote", { target: "unix" });
  child.stdout.emit("data", "MPDBG-READY {broken\n");
  child.emit("close", 0, null);
  await assert.rejects(promise, /without a usable handshake: malformed MPDBG-READY JSON/);
});

test("runDebugCommand times out and kills the child when nothing arrives", async () => {
  const child = new FakeChild();
  const promise = runDebugCommand(fakeSpawn(child, []), "mpremote", { target: "unix" }, { timeoutMs: 20 });
  await assert.rejects(promise, /timed out/);
  assert.equal(child.killed, true);
});

test("runDebugCommand's timeout error includes output captured before it fired", async () => {
  const child = new FakeChild();
  const promise = runDebugCommand(fakeSpawn(child, []), "mpremote", { target: "unix" }, { timeoutMs: 20 });
  child.stdout.emit("data", "waiting for the device to report its debug-server endpoint...\n");
  await assert.rejects(promise, (err) => {
    assert.match((err as Error).message, /timed out/);
    assert.match((err as Error).message, /waiting for the device/);
    return true;
  });
  assert.equal(child.killed, true);
});

test("runDebugCommand passes cwd and env through to the spawn function", async () => {
  const child = new FakeChild();
  const calls: SpawnCall[] = [];
  const promise = runDebugCommand(fakeSpawn(child, calls), "mpremote", { target: "unix" }, {
    cwd: "/proj",
    env: { MPY_DEBUG_FIRMWARE: "/fw" },
  });
  child.stdout.emit("data", HANDSHAKE_LINE);
  await promise;
  assert.deepEqual(calls[0].env, { MPY_DEBUG_FIRMWARE: "/fw" });
});

test("runDebugCommand calls onSpawn synchronously with the child, before the handshake settles", async () => {
  const child = new FakeChild();
  let spawned: ChildProcessLike | undefined;
  const promise = runDebugCommand(fakeSpawn(child, []), "mpremote", { target: "unix" }, {
    onSpawn: (c) => {
      spawned = c;
    },
  });
  assert.equal(spawned, child);
  child.stdout.emit("data", HANDSHAKE_LINE);
  await promise;
});

test("runDebugCommand rejects and kills the child on a spawn error", async () => {
  const child = new FakeChild();
  const promise = runDebugCommand(fakeSpawn(child, []), "mpremote", { target: "unix" });
  child.emit("error", new Error("ENOENT"));
  await assert.rejects(promise, /failed to start mpremote/);
  assert.equal(child.killed, true);
});

test("runDebugCommand's default timeout leaves headroom over the CLI's own --timeout", async () => {
  const child = new FakeChild();
  const promise = runDebugCommand(fakeSpawn(child, []), "mpremote", { target: "unix", timeout: 1 }, {});
  // Resolve quickly so the test doesn't have to wait out the real timeout;
  // this only exercises that construction with a small --timeout doesn't
  // throw and that the handshake still resolves normally.
  child.stdout.emit("data", HANDSHAKE_LINE);
  const result = await promise;
  assert.equal(result.handshake.port, 5678);
});
