import assert from "node:assert/strict";
import { test } from "node:test";

import { HandshakeScanner, parseHandshakeLine } from "../handshake";

const LINE = 'MPDBG-READY {"host": "127.0.0.1", "port": 5678, "caps": {"settrace": true, "set_local": false}}';

test("parseHandshakeLine accepts a well-formed line", () => {
  const h = parseHandshakeLine(LINE);
  assert.deepEqual(h, {
    host: "127.0.0.1",
    port: 5678,
    caps: { settrace: true, set_local: false },
  });
});

test("parseHandshakeLine rejects a missing prefix", () => {
  assert.throws(() => parseHandshakeLine('{"host": "x", "port": 1, "caps": {}}'));
});

test("parseHandshakeLine rejects malformed JSON", () => {
  assert.throws(() => parseHandshakeLine("MPDBG-READY {not json"), /malformed/i);
});

test("parseHandshakeLine rejects a non-integer port", () => {
  assert.throws(
    () => parseHandshakeLine('MPDBG-READY {"host": "x", "port": 1.5, "caps": {}}'),
    /port/i
  );
});

test("parseHandshakeLine rejects an out-of-range port", () => {
  assert.throws(
    () => parseHandshakeLine('MPDBG-READY {"host": "x", "port": 70000, "caps": {}}'),
    /port/i
  );
});

test("parseHandshakeLine rejects a missing caps object", () => {
  assert.throws(() => parseHandshakeLine('MPDBG-READY {"host": "x", "port": 1}'), /caps/i);
});

test("parseHandshakeLine rejects a non-boolean cap value", () => {
  assert.throws(
    () => parseHandshakeLine('MPDBG-READY {"host": "x", "port": 1, "caps": {"settrace": "yes"}}'),
    /caps/i
  );
});

test("parseHandshakeLine rejects an empty host", () => {
  assert.throws(
    () => parseHandshakeLine('MPDBG-READY {"host": "", "port": 1, "caps": {}}'),
    /host/i
  );
});

test("HandshakeScanner reassembles a line split mid-chunk", () => {
  const scanner = new HandshakeScanner();
  assert.equal(scanner.push(LINE.slice(0, 20)), undefined);
  const found = scanner.push(LINE.slice(20) + "\n");
  assert.deepEqual(found, { host: "127.0.0.1", port: 5678, caps: { settrace: true, set_local: false } });
});

test("HandshakeScanner handles several lines in one chunk", () => {
  const lines: string[] = [];
  const scanner = new HandshakeScanner((line) => lines.push(line));
  const found = scanner.push(`booting...\nready\n${LINE}\ntrailer\n`);
  assert.ok(found);
  assert.deepEqual(lines, ["booting...", "ready", "trailer"]);
});

test("HandshakeScanner strips CRLF line endings", () => {
  const lines: string[] = [];
  const scanner = new HandshakeScanner((line) => lines.push(line));
  const found = scanner.push(`noise\r\n${LINE}\r\n`);
  assert.ok(found);
  assert.deepEqual(lines, ["noise"]);
});

test("HandshakeScanner ignores a second MPDBG-READY line and passes through garbage after it", () => {
  const lines: string[] = [];
  const scanner = new HandshakeScanner((line) => lines.push(line));
  const other = 'MPDBG-READY {"host": "10.0.0.1", "port": 1, "caps": {}}';
  const found = scanner.push(`before\n${LINE}\n${other}\nafter\n`);
  assert.deepEqual(found, { host: "127.0.0.1", port: 5678, caps: { settrace: true, set_local: false } });
  assert.deepEqual(lines, ["before", "after"]);
});

test("HandshakeScanner surfaces garbage before and after the handshake", () => {
  const lines: string[] = [];
  const scanner = new HandshakeScanner((line) => lines.push(line));
  scanner.push(`junk1\njunk2\n${LINE}\nmore output\n`);
  assert.deepEqual(lines, ["junk1", "junk2", "more output"]);
});

test("HandshakeScanner keeps scanning past a malformed prefixed line", () => {
  const lines: string[] = [];
  const scanner = new HandshakeScanner((line) => lines.push(line));
  const found = scanner.push(`MPDBG-READY {broken\n${LINE}\n`);
  assert.deepEqual(found, { host: "127.0.0.1", port: 5678, caps: { settrace: true, set_local: false } });
  assert.match(scanner.malformed!.message, /malformed/i);
  assert.deepEqual(lines, ["MPDBG-READY {broken"]);
});

test("HandshakeScanner reports the malformed line when no handshake ever arrives", () => {
  const scanner = new HandshakeScanner();
  assert.equal(scanner.push("MPDBG-READY {broken\nother output\n"), undefined);
  assert.match(scanner.malformed!.message, /malformed MPDBG-READY JSON/);
});

test("HandshakeScanner caps its buffer instead of growing unbounded on a line with no newline", () => {
  const scanner = new HandshakeScanner();
  scanner.push("x".repeat(100_000)); // no "\n": simulates \r-only progress output
  const found = scanner.push(`\n${LINE}\n`);
  assert.deepEqual(found, { host: "127.0.0.1", port: 5678, caps: { settrace: true, set_local: false } });
});

test("HandshakeScanner.flush treats an unterminated remainder as a final line", () => {
  const scanner = new HandshakeScanner();
  scanner.push(LINE); // no trailing newline: source closed mid-line
  const found = scanner.flush();
  assert.deepEqual(found, { host: "127.0.0.1", port: 5678, caps: { settrace: true, set_local: false } });
});

test("parseHandshakeLine accepts pathMappings field", () => {
  const line = 'MPDBG-READY {"host": "127.0.0.1", "port": 5678, "caps": {}, "pathMappings": [{"localRoot": "/home/dev/src", "remoteRoot": "/remote"}]}';
  const h = parseHandshakeLine(line);
  assert.deepEqual(h.pathMappings, [{ localRoot: "/home/dev/src", remoteRoot: "/remote" }]);
});

test("parseHandshakeLine rejects pathMappings as non-array", () => {
  const line = 'MPDBG-READY {"host": "127.0.0.1", "port": 5678, "caps": {}, "pathMappings": "not-an-array"}';
  assert.throws(() => parseHandshakeLine(line), /pathMappings.*array/i);
});

test("parseHandshakeLine rejects pathMappings entry missing localRoot", () => {
  const line = 'MPDBG-READY {"host": "127.0.0.1", "port": 5678, "caps": {}, "pathMappings": [{"remoteRoot": "/remote"}]}';
  assert.throws(() => parseHandshakeLine(line), /localRoot.*non-empty string/i);
});

test("parseHandshakeLine rejects pathMappings entry missing remoteRoot", () => {
  const line = 'MPDBG-READY {"host": "127.0.0.1", "port": 5678, "caps": {}, "pathMappings": [{"localRoot": "/home"}]}';
  assert.throws(() => parseHandshakeLine(line), /remoteRoot.*non-empty string/i);
});

test("parseHandshakeLine rejects pathMappings entry with empty localRoot", () => {
  const line = 'MPDBG-READY {"host": "127.0.0.1", "port": 5678, "caps": {}, "pathMappings": [{"localRoot": "", "remoteRoot": "/remote"}]}';
  assert.throws(() => parseHandshakeLine(line), /localRoot.*non-empty string/i);
});

test("parseHandshakeLine rejects pathMappings entry with empty remoteRoot", () => {
  const line = 'MPDBG-READY {"host": "127.0.0.1", "port": 5678, "caps": {}, "pathMappings": [{"localRoot": "/home", "remoteRoot": ""}]}';
  assert.throws(() => parseHandshakeLine(line), /remoteRoot.*non-empty string/i);
});

test("parseHandshakeLine accepts multiple pathMappings entries", () => {
  const line = 'MPDBG-READY {"host": "127.0.0.1", "port": 5678, "caps": {}, "pathMappings": [{"localRoot": "/home/src", "remoteRoot": "/remote"}, {"localRoot": "/home/tests", "remoteRoot": "/test-remote"}]}';
  const h = parseHandshakeLine(line);
  assert.deepEqual(h.pathMappings, [
    { localRoot: "/home/src", remoteRoot: "/remote" },
    { localRoot: "/home/tests", remoteRoot: "/test-remote" },
  ]);
});

test("parseHandshakeLine omits pathMappings when absent", () => {
  const line = 'MPDBG-READY {"host": "127.0.0.1", "port": 5678, "caps": {}}';
  const h = parseHandshakeLine(line);
  assert.equal(h.pathMappings, undefined);
});

test("parseHandshakeLine rejects pathMappings entry that is not an object", () => {
  const line = 'MPDBG-READY {"host": "127.0.0.1", "port": 5678, "caps": {}, "pathMappings": ["not-an-object"]}';
  assert.throws(() => parseHandshakeLine(line), /pathMappings.*must be an object/i);
});

test("HandshakeScanner extracts pathMappings from multi-root handshake", () => {
  const lines: string[] = [];
  const scanner = new HandshakeScanner((line) => lines.push(line));
  const line = 'MPDBG-READY {"host": "127.0.0.1", "port": 5678, "caps": {}, "pathMappings": [{"localRoot": "/a", "remoteRoot": "/b"}]}';
  const found = scanner.push(line + "\n");
  assert.deepEqual(found?.pathMappings, [{ localRoot: "/a", remoteRoot: "/b" }]);
});
