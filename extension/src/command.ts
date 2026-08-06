/**
 * Builds and runs the `mpremote debug` child process.
 *
 * Pure argv construction plus a process runner that takes an injected spawn
 * function, so tests drive it with a fake instead of a real child process.
 * No `vscode` import.
 */

import { StringDecoder } from "node:string_decoder";

import { Handshake, HandshakeScanner } from "./handshake";

export interface MpremoteDebugOptions {
  /** Name from mpdebug.toml, "unix", or an mpremote connect string. */
  target?: string;
  /** "module[:method]". */
  program?: string;
  port?: number;
  /** Seconds; mirrors mpremote debug's own --timeout (default 60). */
  timeout?: number;
  dapLog?: boolean;
  dapLogFile?: string;
}

/**
 * Builds argv for `mpremote debug`, options strictly before positionals -
 * `mpremote debug [--port N] [--timeout N] [--dap-log] [--dap-log-file F]
 * [target] [program]`. The returned array starts with "debug"; the caller
 * supplies the executable (`mpremote`) itself.
 */
export function buildDebugArgs(options: MpremoteDebugOptions): string[] {
  if (options.dapLogFile !== undefined && !options.dapLog) {
    throw new Error("dapLogFile requires dapLog");
  }
  // mpremote's argparse fills the target/program positionals left to right;
  // a program with no target can't be expressed on the CLI.
  if (options.program !== undefined && options.target === undefined) {
    throw new Error("program requires target: mpremote debug can't skip the target positional");
  }

  const args: string[] = ["debug"];
  if (options.port !== undefined) {
    args.push("--port", String(options.port));
  }
  if (options.timeout !== undefined) {
    args.push("--timeout", String(options.timeout));
  }
  if (options.dapLog) {
    args.push("--dap-log");
  }
  if (options.dapLogFile !== undefined) {
    args.push("--dap-log-file", options.dapLogFile);
  }
  if (options.target !== undefined) {
    args.push(options.target);
  }
  if (options.program !== undefined) {
    args.push(options.program);
  }
  return args;
}

/** The slice of Node's ChildProcess/stream API this module depends on. */
export interface ReadableLike {
  on(event: "data", listener: (chunk: Buffer | string) => void): void;
  removeListener(event: "data", listener: (chunk: Buffer | string) => void): void;
}

export interface ChildProcessLike {
  stdout: ReadableLike;
  stderr?: ReadableLike;
  // "close" fires once the process has exited AND its stdio streams have
  // ended, unlike "exit" (which can fire before buffered stdout is
  // delivered to listeners) - the terminal signal this module waits on.
  on(event: "close" | "error", listener: (...args: any[]) => void): void;
  removeListener(event: "close" | "error", listener: (...args: any[]) => void): void;
  kill(signal?: string): boolean;
}

export type SpawnFn = (
  command: string,
  args: string[],
  options: { cwd?: string; env?: Record<string, string> }
) => ChildProcessLike;

export interface RunDebugResult {
  handshake: Handshake;
  /**
   * The live child. Unix stays attached to it for the whole session; a
   * device transport has already exited normally by the time this
   * resolves unless --dap-log keeps a proxy alive. Ending the debug
   * session must still terminate it where it is still running.
   */
  child: ChildProcessLike;
}

export interface RunDebugCommandExtra {
  /**
   * Arguments placed before "debug", so `mpremotePath` can be an interpreter
   * rather than the installed CLI - `python3 -m mpremote` against a source
   * checkout, which is the only way to reach the command before it is
   * released.
   */
  argsPrefix?: string[];
  cwd?: string;
  /** Extra environment variables, merged over the inherited environment. */
  env?: Record<string, string>;
  /** JS-side wait for the handshake; default derives from options.timeout. */
  timeoutMs?: number;
  /**
   * Called synchronously right after the child is spawned, before the
   * handshake wait begins, so a caller can track the child (e.g. to reap it
   * on shutdown) without waiting for this function's promise to settle.
   */
  onSpawn?: (child: ChildProcessLike) => void;
}

const DEFAULT_CLI_TIMEOUT_S = 60; // mpremote debug's own --timeout default
const TIMEOUT_MARGIN_MS = 5_000; // let the CLI's own --timeout fire first

// Diagnostic output is capped: a boot-looping device or a chatty child can
// otherwise grow the captured-output error message (surfaced verbatim in an
// error toast) without bound. The full stream still reaches the caller's own
// listeners (e.g. an output channel) independently of this cap.
const MAX_OUTPUT_LINES = 200;
const MAX_LINE_CHARS = 4_000;

function pushOutput(lines: string[], text: string): void {
  const trimmed = text.length > MAX_LINE_CHARS ? `${text.slice(0, MAX_LINE_CHARS)}...[truncated]` : text;
  lines.push(trimmed);
  if (lines.length > MAX_OUTPUT_LINES) {
    lines.shift();
  }
}

/**
 * Spawns `mpremote debug` via `spawnFn` and resolves with `{handshake,
 * child}` once the `MPDBG-READY` line arrives. Rejects with a diagnostic
 * error (captured stdout included) on early exit, a malformed handshake, a
 * spawn error, or a timeout - and kills the child on every rejection path,
 * since the caller never gets a handle to it in that case.
 */
export function runDebugCommand(
  spawnFn: SpawnFn,
  mpremotePath: string,
  options: MpremoteDebugOptions,
  extra: RunDebugCommandExtra = {}
): Promise<RunDebugResult> {
  const args = [...(extra.argsPrefix ?? []), ...buildDebugArgs(options)];
  const timeoutMs =
    extra.timeoutMs ?? (options.timeout ?? DEFAULT_CLI_TIMEOUT_S) * 1000 + TIMEOUT_MARGIN_MS;

  return new Promise((resolve, reject) => {
    let settled = false;
    const outputLines: string[] = [];
    const scanner = new HandshakeScanner((line) => pushOutput(outputLines, line));
    const stdoutDecoder = new StringDecoder("utf8");
    const stderrDecoder = new StringDecoder("utf8");
    const child = spawnFn(mpremotePath, args, { cwd: extra.cwd, env: extra.env });
    extra.onSpawn?.(child);

    const timer = setTimeout(() => {
      const malformed = scanner.malformed;
      settleReject(
        withCaptured(
          malformed
            ? `timed out after ${timeoutMs}ms; the one MPDBG-READY line seen was unusable: ${malformed.message}`
            : `timed out after ${timeoutMs}ms waiting for the debug handshake`
        )
      );
    }, timeoutMs);

    function withCaptured(message: string): Error {
      return new Error(outputLines.length ? `${message}\n${outputLines.join("\n")}` : message);
    }

    function decode(decoder: StringDecoder, chunk: Buffer | string): string {
      return typeof chunk === "string" ? chunk : decoder.write(chunk);
    }

    function cleanupListeners(): void {
      clearTimeout(timer);
      child.stdout.removeListener("data", onStdout);
      child.stderr?.removeListener("data", onStderr);
      child.removeListener("close", onClose);
      child.removeListener("error", onError);
    }

    function settleResolve(handshake: Handshake): void {
      if (settled) {
        return;
      }
      settled = true;
      cleanupListeners();
      // The caller now owns the child's lifetime; keep at least one
      // "error" listener attached so a later error (e.g. an EPERM on
      // kill()) doesn't become an unhandled "error" event.
      child.on("error", () => {});
      resolve({ handshake, child });
    }

    function settleReject(err: Error): void {
      if (settled) {
        return;
      }
      settled = true;
      cleanupListeners();
      try {
        child.kill();
      } catch {
        // already gone
      }
      reject(err);
    }

    function onStdout(chunk: Buffer | string): void {
      const handshake = scanner.push(decode(stdoutDecoder, chunk));
      if (handshake) {
        settleResolve(handshake);
      }
    }

    function onStderr(chunk: Buffer | string): void {
      pushOutput(outputLines, decode(stderrDecoder, chunk));
    }

    function onClose(code: number | null, signal: string | null): void {
      const tail = stdoutDecoder.end();
      if (tail) {
        scanner.push(tail);
      }
      const handshake = scanner.flush();
      if (handshake) {
        settleResolve(handshake);
        return;
      }
      settleReject(withCaptured(endOfStreamMessage(signal ? `signal ${signal}` : `code ${code}`)));
    }

    /**
     * Why no handshake arrived. A prefix-matching line that failed to parse
     * is the more useful answer than the bare exit status, so it leads.
     */
    function endOfStreamMessage(how: string): string {
      const malformed = scanner.malformed;
      if (malformed) {
        return `mpremote exited (${how}) without a usable handshake: ${malformed.message}`;
      }
      return `mpremote exited (${how}) before reporting a debug handshake`;
    }

    function onError(err: Error): void {
      settleReject(withCaptured(`failed to start ${mpremotePath}: ${err.message}`));
    }

    child.stdout.on("data", onStdout);
    child.stderr?.on("data", onStderr);
    child.on("close", onClose);
    child.on("error", onError);
  });
}
