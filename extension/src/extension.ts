/**
 * VS Code glue: registers the `micropython` debug type and resolves it into
 * a `debugpy` attach session backed by a spawned `mpremote debug`.
 *
 * All orchestration (firmware/target selection, source sync, endpoint
 * discovery) lives in the `mpremote debug` command; this file only spawns
 * it, parses its handshake, and hands the result to VS Code's Python
 * debugger. It never runs a `micropython` debug adapter itself -
 * `resolveDebugConfigurationWithSubstitutedVariables` always redirects to a
 * `debugpy` session and returns `undefined`.
 */

import * as cp from "child_process";
import * as vscode from "vscode";

import { buildAttachConfig, summarizeCapabilities } from "./config";
import { ChildProcessLike, MpremoteDebugOptions, SpawnFn, runDebugCommand } from "./command";
import { Handshake } from "./handshake";
import { TargetPicker } from "./targetPicker";
import { discoverTargets } from "./targets";

const DEBUGPY_EXTENSION_ID = "ms-python.debugpy";

interface MicroPythonLaunchConfig extends vscode.DebugConfiguration {
  target?: string;
  program?: string;
  port?: number;
  timeout?: number;
  dapLog?: boolean;
  dapLogFile?: string;
  cwd?: string;
  mpremotePath?: string;
  mpremoteArgs?: string[];
  env?: Record<string, string>;
}

// Keyed by vscode.DebugSession.id: the mpremote child backing that session,
// so ending the session (however it ends) terminates the process that owns
// the debug-server socket.
const sessionChildren = new Map<string, ChildProcessLike>();
// Children spawned but not yet attributed to a session - covers the window
// between spawn and a settled resolve (including a VS Code shutdown mid
// handshake wait), so deactivate() can still reap them.
const pendingChildren = new Set<ChildProcessLike>();
let deactivated = false;

function terminateChild(child: ChildProcessLike): void {
  try {
    child.kill();
  } catch {
    // already gone
  }
}

function makeSpawnFn(channel: vscode.OutputChannel): SpawnFn {
  return (command, args, options) => {
    channel.appendLine(`$ ${command} ${args.join(" ")}`);
    const child = cp.spawn(command, args, {
      cwd: options.cwd,
      env: options.env ? { ...process.env, ...options.env } : undefined,
    });
    // Streams for the child's whole lifetime, independent of
    // runDebugCommand's own listeners (which it detaches once resolved).
    child.stdout?.on("data", (chunk) => channel.append(chunk.toString()));
    child.stderr?.on("data", (chunk) => channel.append(chunk.toString()));
    child.on("error", (err) => channel.appendLine(`mpremote process error: ${err.message}`));
    return child as unknown as ChildProcessLike;
  };
}

/**
 * A launched session is correlated to its child primarily via the
 * `mpyDebugLaunchId` marker this module stamps onto the attach config. If a
 * downstream resolver (debugpy's own) returns a fresh config object rather
 * than mutating the one it was given, that marker is lost; the connect port
 * from the handshake - unique per launch in practice - is the fallback.
 */
function matchesLaunch(session: vscode.DebugSession, launchId: string, port: number): boolean {
  if (session.configuration.mpyDebugLaunchId === launchId) {
    return true;
  }
  const connect = session.configuration.connect as { port?: number } | undefined;
  return session.type === "debugpy" && connect?.port === port;
}

class MicroPythonConfigurationProvider implements vscode.DebugConfigurationProvider {
  constructor(
    private readonly channel: vscode.OutputChannel,
    private readonly targetPicker: TargetPicker
  ) {}

  provideDebugConfigurations(
    _folder: vscode.WorkspaceFolder | undefined
  ): vscode.ProviderResult<vscode.DebugConfiguration[]> {
    return [
      {
        type: "micropython",
        request: "launch",
        name: "MicroPython: Debug",
        cwd: "${workspaceFolder}",
      },
    ];
  }

  // Cheap validation/defaulting only: VS Code calls this BEFORE variables
  // like ${workspaceFolder} are substituted, so anything that needs their
  // expanded values belongs in resolveDebugConfigurationWithSubstitutedVariables
  // instead.
  resolveDebugConfiguration(
    _folder: vscode.WorkspaceFolder | undefined,
    config: MicroPythonLaunchConfig
  ): vscode.ProviderResult<vscode.DebugConfiguration> {
    return config;
  }

  async resolveDebugConfigurationWithSubstitutedVariables(
    folder: vscode.WorkspaceFolder | undefined,
    config: MicroPythonLaunchConfig,
    token?: vscode.CancellationToken
  ): Promise<vscode.DebugConfiguration | undefined> {
    if (!vscode.extensions.getExtension(DEBUGPY_EXTENSION_ID)) {
      vscode.window.showErrorMessage(
        `MicroPython debugging requires the "${DEBUGPY_EXTENSION_ID}" extension. ` +
          "Install it from the Marketplace, then start the session again."
      );
      return undefined;
    }
    if (!folder) {
      vscode.window.showErrorMessage("MicroPython debugging needs a workspace folder open.");
      return undefined;
    }

    const cwd = config.cwd ?? folder.uri.fsPath;
    // An explicit `target` attribute always wins; the picker's persisted
    // selection is only the fallback for a launch config that omits it.
    const target = config.target ?? this.targetPicker.selectedTarget;
    this.targetPicker.reportSessionStart(target);
    const options: MpremoteDebugOptions = {
      target,
      program: config.program,
      port: config.port,
      timeout: config.timeout,
      dapLog: config.dapLog,
      dapLogFile: config.dapLogFile,
    };

    let child: ChildProcessLike | undefined;
    let handshake: Handshake;
    this.channel.show(true);
    try {
      const result = await runDebugCommand(
        makeSpawnFn(this.channel),
        config.mpremotePath || "mpremote",
        options,
        {
          argsPrefix: config.mpremoteArgs,
          cwd,
          env: config.env,
          onSpawn: (spawned) => {
            child = spawned;
            pendingChildren.add(spawned);
          },
        }
      );
      handshake = result.handshake;
    } catch (err) {
      if (!token?.isCancellationRequested) {
        const message = (err as Error).message;
        this.channel.appendLine(message);
        vscode.window.showErrorMessage(`MicroPython debug: ${message}`);
      }
      return undefined;
    } finally {
      if (child) {
        pendingChildren.delete(child);
      }
    }

    if (!child) {
      // Unreachable: onSpawn (above) runs synchronously before
      // runDebugCommand resolves, so a run that didn't throw always set it.
      return undefined;
    }
    // Rebound to a `const`: closures below capture this, and TS can't
    // narrow a captured `let` to non-undefined the way it narrows `child`
    // in this function's own body.
    const mpremoteChild: ChildProcessLike = child;

    if (deactivated || token?.isCancellationRequested) {
      terminateChild(mpremoteChild);
      return undefined;
    }

    for (const note of summarizeCapabilities(handshake.caps)) {
      this.channel.appendLine(note);
    }
    this.targetPicker.reportHandshake(target, handshake.caps);

    // handshake.pathMappings is mpremote's own generated mapping - a unix
    // target's identity mapping, or the absolute source root a serial/network
    // target mounted - and takes precedence whenever present. The
    // localRoot/remoteRoot fallback below only fires for a handshake from an
    // mpremote build old enough to predate that field.
    //
    // `target` is a name, not a transport - a `[target.<name>]` in
    // mpdebug.toml can call a "serial" target "unix", or vice versa - so
    // its `kind` (when the name resolves to a configured target) decides
    // this, mirroring `mpdebug_config.resolve_target`. A name that isn't a
    // configured target falls back to the literal "unix" connect string,
    // exactly as the command itself does.
    const discovered = discoverTargets(cwd);
    const resolvedKind = discovered.ok ? discovered.targets.find((t) => t.name === target)?.kind : undefined;
    const isUnixFlow = resolvedKind !== undefined ? resolvedKind === "unix" : target === "unix";
    const attachConfig = buildAttachConfig({
      handshake,
      name: config.name,
      pathMappings: handshake.pathMappings,
      ...(isUnixFlow ? { localRoot: cwd, remoteRoot: cwd } : {}),
    });

    const launchId = `${Date.now()}-${Math.random().toString(36).slice(2)}`;
    let matched = false;
    const startedSub = vscode.debug.onDidStartDebugSession((session) => {
      if (matchesLaunch(session, launchId, handshake.port)) {
        sessionChildren.set(session.id, mpremoteChild);
        matched = true;
      }
    });

    try {
      const started = await vscode.debug.startDebugging(folder, {
        ...attachConfig,
        mpyDebugLaunchId: launchId,
      });
      if (!started) {
        terminateChild(mpremoteChild);
        vscode.window.showErrorMessage("MicroPython debug: the debugpy attach session failed to start.");
      } else if (!matched) {
        // Nothing will reap it on session end, so keep it in the pending set
        // for deactivate() rather than dropping the only handle to it.
        pendingChildren.add(mpremoteChild);
        this.channel.appendLine(
          "warning: could not correlate the started debugpy session with its mpremote " +
            "process; it will be terminated on shutdown rather than at session end."
        );
      }
    } catch (err) {
      terminateChild(mpremoteChild);
      vscode.window.showErrorMessage(
        `MicroPython debug: failed to start the attach session: ${(err as Error).message}`
      );
    } finally {
      startedSub.dispose();
    }

    // A resolver can never change a config's type: the caller already has
    // its debugpy session (or the failure above), and the "micropython"
    // config that triggered this resolve must not itself be launched.
    return undefined;
  }
}

/**
 * `activate()`'s return value, VS Code's `Extension.exports` - read by the
 * host test suite. `workspaceState` is exposed alongside `targetPicker` so
 * tests can assert on the memento VS Code itself persists
 * (`SELECTED_TARGET_STATE_KEY`) rather than only on the picker's own getter.
 */
export interface ExtensionApi {
  targetPicker: TargetPicker;
  workspaceState: vscode.Memento;
}

export function activate(context: vscode.ExtensionContext): ExtensionApi {
  const channel = vscode.window.createOutputChannel("MicroPython Debug");
  const targetPicker = new TargetPicker(context, channel);
  const provider = new MicroPythonConfigurationProvider(channel, targetPicker);

  context.subscriptions.push(
    channel,
    targetPicker,
    vscode.debug.registerDebugConfigurationProvider("micropython", provider),
    vscode.debug.registerDebugConfigurationProvider(
      "micropython",
      provider,
      vscode.DebugConfigurationProviderTriggerKind.Dynamic
    ),
    vscode.debug.onDidTerminateDebugSession((session) => {
      const child = sessionChildren.get(session.id);
      if (child) {
        terminateChild(child);
        sessionChildren.delete(session.id);
      }
    })
  );

  return { targetPicker, workspaceState: context.workspaceState };
}

export function deactivate(): void {
  deactivated = true;
  for (const child of sessionChildren.values()) {
    terminateChild(child);
  }
  sessionChildren.clear();
  for (const child of pendingChildren.values()) {
    terminateChild(child);
  }
  pendingChildren.clear();
}
