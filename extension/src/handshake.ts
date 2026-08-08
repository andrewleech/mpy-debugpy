/**
 * Parses `mpremote debug`'s `MPDBG-READY {json}` handshake line.
 *
 * Pure: no `vscode` or Node process/child_process imports, so it is
 * unit-testable without a VS Code host and reusable by anything reading the
 * command's stdout (the extension resolver, a test harness).
 */

export const HANDSHAKE_PREFIX = "MPDBG-READY ";

/** One local/remote directory pair, always absolute on both sides. */
export interface PathMapping {
  localRoot: string;
  remoteRoot: string;
}

export interface Handshake {
  host: string;
  port: number;
  caps: Record<string, boolean>;
  /**
   * The command's own generated mapping between the remote path the target
   * imports under and the local directory that is its source - a unix
   * target's identity mapping, or the absolute host directory a serial/network
   * target mounted. Absent on a handshake from an mpremote build that
   * predates this field, which callers must treat exactly like an empty
   * list: fall back to whatever mapping (if any) they synthesized before
   * this field existed.
   */
  pathMappings?: PathMapping[];
}

/**
 * Parses one complete line (no trailing newline) as an `MPDBG-READY`
 * handshake. Throws if the line doesn't carry the prefix, its payload isn't
 * valid JSON, or the JSON doesn't match the handshake shape (`host` a
 * non-empty string, `port` an integer 1..65535, `caps` an object of
 * booleans, `pathMappings` when present a list of absolute local/remote
 * string pairs) - a malformed handshake is a defect worth reporting, never a
 * silent skip.
 */
export function parseHandshakeLine(line: string): Handshake {
  if (!line.startsWith(HANDSHAKE_PREFIX)) {
    throw new Error(`not an MPDBG-READY line: ${line}`);
  }
  const payload = line.slice(HANDSHAKE_PREFIX.length);
  let data: unknown;
  try {
    data = JSON.parse(payload);
  } catch (err) {
    throw new Error(`malformed MPDBG-READY JSON (${(err as Error).message}): ${payload}`);
  }
  return validate(data, payload);
}

function validatePathMappings(raw: unknown, payload: string): PathMapping[] | undefined {
  if (raw === undefined) {
    return undefined;
  }
  if (!Array.isArray(raw)) {
    throw new Error(`malformed MPDBG-READY payload, "pathMappings" must be an array: ${payload}`);
  }
  return raw.map((entry, i) => {
    if (typeof entry !== "object" || entry === null || Array.isArray(entry)) {
      throw new Error(
        `malformed MPDBG-READY payload, pathMappings[${i}] must be an object: ${payload}`
      );
    }
    const e = entry as Record<string, unknown>;
    if (typeof e.localRoot !== "string" || e.localRoot.length === 0) {
      throw new Error(
        `malformed MPDBG-READY payload, pathMappings[${i}].localRoot must be a non-empty string: ${payload}`
      );
    }
    if (typeof e.remoteRoot !== "string" || e.remoteRoot.length === 0) {
      throw new Error(
        `malformed MPDBG-READY payload, pathMappings[${i}].remoteRoot must be a non-empty string: ${payload}`
      );
    }
    return { localRoot: e.localRoot, remoteRoot: e.remoteRoot };
  });
}

function validate(data: unknown, payload: string): Handshake {
  if (typeof data !== "object" || data === null || Array.isArray(data)) {
    throw new Error(`malformed MPDBG-READY payload, not an object: ${payload}`);
  }
  const obj = data as Record<string, unknown>;

  const host = obj.host;
  if (typeof host !== "string" || host.length === 0) {
    throw new Error(`malformed MPDBG-READY payload, "host" must be a non-empty string: ${payload}`);
  }

  const port = obj.port;
  if (typeof port !== "number" || !Number.isInteger(port) || port < 1 || port > 65535) {
    throw new Error(`malformed MPDBG-READY payload, "port" must be an integer 1..65535: ${payload}`);
  }

  const caps = obj.caps;
  if (typeof caps !== "object" || caps === null || Array.isArray(caps)) {
    throw new Error(`malformed MPDBG-READY payload, "caps" must be an object: ${payload}`);
  }
  const capsObj = caps as Record<string, unknown>;
  for (const [key, value] of Object.entries(capsObj)) {
    if (typeof value !== "boolean") {
      throw new Error(
        `malformed MPDBG-READY payload, caps["${key}"] must be a boolean: ${payload}`
      );
    }
  }

  const pathMappings = validatePathMappings(obj.pathMappings, payload);

  return {
    host,
    port,
    caps: capsObj as Record<string, boolean>,
    ...(pathMappings !== undefined ? { pathMappings } : {}),
  };
}

// A handshake line is short and bounded; a source that never emits a
// newline (e.g. \r-only progress output) must not grow the scanner's
// buffer unbounded, so only the most recent slice is kept.
const MAX_BUF_CHARS = 64 * 1024;

/**
 * Incremental line scanner fed arbitrary stdout chunks - split mid-line,
 * several lines per chunk, CRLF - that surfaces the first valid handshake
 * and every other line it saw (for diagnostics) via the `onLine` callback.
 *
 * A second `MPDBG-READY` line, after the first has already been accepted,
 * is dropped rather than re-parsed or reported: the first handshake is
 * authoritative for the life of the scanner.
 *
 * A prefix-matching line that does NOT parse is kept as a diagnostic and
 * scanning continues. The unix flow echoes the debuggee's own stdout, so
 * this stream carries lines from a source that never agreed to the
 * handshake contract; killing a working session over one of them would be
 * wrong. `malformed` lets a caller that reaches end-of-stream without a
 * handshake name the likely cause instead of reporting a bare timeout.
 */
export class HandshakeScanner {
  private buf = "";
  private found: Handshake | undefined;
  private malformedError: Error | undefined;

  constructor(private readonly onLine?: (line: string) => void) {}

  /**
   * The first `MPDBG-READY`-prefixed line that failed to parse, if any.
   * Only meaningful once the stream has ended with no handshake.
   */
  get malformed(): Error | undefined {
    return this.malformedError;
  }

  /** Feeds a chunk of raw text. Returns the handshake once it is complete. */
  push(chunk: string): Handshake | undefined {
    this.buf += chunk;
    let idx: number;
    while ((idx = this.buf.indexOf("\n")) !== -1) {
      const line = this.buf.slice(0, idx);
      this.buf = this.buf.slice(idx + 1);
      this.consumeLine(line.endsWith("\r") ? line.slice(0, -1) : line);
    }
    if (this.buf.length > MAX_BUF_CHARS) {
      this.buf = this.buf.slice(-MAX_BUF_CHARS);
    }
    return this.found;
  }

  /**
   * Treats any buffered, not-yet-newline-terminated remainder as a final
   * line - for when the source (e.g. a child process's stdout) has closed
   * without a trailing newline.
   */
  flush(): Handshake | undefined {
    if (this.buf.length > 0) {
      const rest = this.buf;
      this.buf = "";
      this.consumeLine(rest);
    }
    return this.found;
  }

  private consumeLine(line: string): void {
    if (line.startsWith(HANDSHAKE_PREFIX) && !this.found) {
      try {
        this.found = parseHandshakeLine(line);
        return;
      } catch (err) {
        this.malformedError ??= err as Error;
      }
    } else if (line.startsWith(HANDSHAKE_PREFIX)) {
      return;
    }
    this.onLine?.(line);
  }
}
