/**
 * Mocha loader for the extension-host suite. Runs inside the Extension
 * Development Host (unlike runHostTests.ts, which launches it); this is
 * what `extensionTestsPath` points at.
 */

import * as fs from "node:fs";
import * as path from "node:path";

import Mocha from "mocha";

export function run(): Promise<void> {
  const mocha = new Mocha({ ui: "bdd", color: false, timeout: 60_000 });
  const testsRoot = __dirname;
  // Sorted: some tests are order-dependent within a file (e.g. an
  // activation check that must run before anything else touches the debug
  // API), and `readdirSync` order is filesystem-dependent otherwise.
  for (const file of fs.readdirSync(testsRoot).filter((f) => f.endsWith(".test.js")).sort()) {
    mocha.addFile(path.join(testsRoot, file));
  }

  return new Promise((resolve, reject) => {
    mocha.run((failures) => {
      if (failures > 0) {
        reject(new Error(`${failures} host test(s) failed.`));
      } else {
        resolve();
      }
    });
  });
}
