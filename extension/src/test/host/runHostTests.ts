/**
 * Entry point for `npm run test:host`. Runs outside VS Code (plain node):
 * downloads/reuses a real VS Code build, installs `ms-python.debugpy` into
 * it, then launches it as an Extension Development Host running the suite
 * in `suite/`.
 *
 * The workspace opened is this repo's own root, not a fixture: it already
 * carries the `.vscode/launch.json` config real users get, and reusing it
 * means the suite exercises the same file the integration tests parse
 * (src/test/integration.test.ts) rather than a copy that can drift from it.
 */

import * as cp from "node:child_process";
import * as fs from "node:fs";
import * as path from "node:path";

import { downloadAndUnzipVSCode, resolveCliArgsFromVSCodeExecutablePath, runTests } from "@vscode/test-electron";

import { readLaunchConfig } from "../launchConfig";

const EXTENSION_DIR = path.resolve(__dirname, "..", "..", "..");
const REPO = path.resolve(EXTENSION_DIR, "..");
const DEBUGPY_EXTENSION_ID = "ms-python.debugpy";
// Pinned rather than "stable": the Dynamic-trigger test drives an internal,
// undocumented command (`workbench.action.debug.selectandstart`) whose
// argument order and activation timing were verified against this exact
// build. A floating version could change that command out from under the
// suite with no diff in this repo to explain the new failure.
const VSCODE_VERSION = "1.132.0";

// The launch.json config always spawns the debug-enabled unix build at this
// path (its own `env.MPY_DEBUG_FIRMWARE` overrides anything set here), so
// this is the one path that answers "will the E2E test's mpremote child
// actually start" - checking any other (e.g. an env override) would pass
// while the real run still fails.
const FIRMWARE = path.join(REPO, "micropython", "ports", "unix", "build-standard", "micropython");

async function main(): Promise<void> {
  if (!fs.existsSync(FIRMWARE)) {
    console.error(
      `No unix debug firmware at ${FIRMWARE}. Run 'make firmware-unix' first - ` +
        "the host suite needs a real target to attach to, not a mock."
    );
    process.exit(1);
  }
  const launchConfig = readLaunchConfig(REPO);
  if ((launchConfig as { type?: string }).type !== "micropython") {
    console.error(".vscode/launch.json's first configuration is not type 'micropython'; nothing to test.");
    process.exit(1);
  }

  // Inert unless this is actually WSL's interop CLI, which otherwise blocks
  // on a stdin prompt ("please install Visual Studio Code in Windows").
  process.env.DONT_PROMPT_WSL_INSTALL = "1";

  // No explicit cachePath: the default (`.vscode-test` under this process's
  // cwd, i.e. extension/ under the `npm run test:host` this script is meant
  // to be invoked from) is also where `resolveCliArgsFromVSCodeExecutablePath`
  // and `runTests` below independently default their own
  // `--extensions-dir`/`--user-data-dir` to - overriding it here would split
  // the installed extension from the profile that's supposed to have it.
  let vscodeExecutablePath: string;
  try {
    vscodeExecutablePath = await downloadAndUnzipVSCode({ version: VSCODE_VERSION });
  } catch (err) {
    // The one case the ticket allows a skip for: no way to get a VS Code
    // build to test against at all (e.g. no network in this environment).
    // On CI that condition is itself a failure - there is no one to notice
    // a silent "0 tests ran" and no interactive session to fall back to.
    console.error(`Could not obtain a VS Code build to test against: ${(err as Error).message}`);
    if (process.env.CI) {
      console.error("::error::running in CI - a download failure here is a failure, not a skip.");
      process.exit(1);
    }
    console.error("No CI env var set; treating this as a local skip rather than a failure.");
    process.exit(0);
  }

  const [cli, ...cliArgs] = resolveCliArgsFromVSCodeExecutablePath(vscodeExecutablePath);
  const install = cp.spawnSync(cli, [...cliArgs, "--install-extension", DEBUGPY_EXTENSION_ID, "--force"], {
    stdio: "inherit",
    env: process.env,
  });
  if (install.status !== 0) {
    console.error(
      `Failed to install ${DEBUGPY_EXTENSION_ID} into the test VS Code build (exit ${install.status}). ` +
        "That install IS the dependency declaration under test; this is a failure, not a skip."
    );
    process.exit(1);
  }

  const exitCode = await runTests({
    vscodeExecutablePath,
    extensionDevelopmentPath: EXTENSION_DIR,
    extensionTestsPath: path.join(EXTENSION_DIR, "out", "test", "host", "suite", "index.js"),
    launchArgs: [REPO, "--disable-workspace-trust", "--skip-welcome", "--skip-release-notes"],
    extensionTestsEnv: {
      ...process.env,
      MPY_DEBUG_FIRMWARE: FIRMWARE,
    },
  });
  process.exit(exitCode);
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
