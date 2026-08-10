# Three CI checks that were wired up and were not running

- Date: 2026-08-10
- Top repo HEAD: `6d0818ea52`
- `micropython`: `4eaafbc5bc` (`mpy-debugpy`)
- `micropython-lib`: `1eb7c82bfd` (`mpy-debugpy`)

Every `firmware` workflow run on `main` had failed, going back to the workflow
existing. Reading them found three separate checks that a reviewer would count
as coverage and a runner never executed, and one recurring way the whole run
dies before any of them.

## The lint gate could not run the linter

`make lint` was `uv run ruff check .`, with ruff in no dependency list. `uv run`
falls back to PATH for a command the environment does not provide, so the
target answered with whichever ruff the machine happened to have - 0.12.1 here,
by accident of a `~/.local/bin` install - and on a machine with none it did not
answer at all:

```
uv run ruff check .
error: Failed to spawn: `ruff`
  Caused by: No such file or directory (os error 2)
make: *** [Makefile:75: lint] Error 2
```

The `unix (standard)` job has ended that way on every run that reached the step
since the gate landed in `b285b4b`: runs `31251545538`, `31251792784`,
`31252488090`, `31264981722` and `31348481999`, 2026-08-08 and 2026-08-10.

The cost was not the lint. The Lint step runs *before* `Run host DAP test suite
against the fresh artifact`, on the stated reasoning that a lint failure is
cheap to report and should not wait behind several minutes of debug sessions -
so a Lint step that could never pass meant the host suite never started. The
last time the host suite ran on a runner was 2026-08-06, run `31120673122`, an
hour and a half before the commit that made ruff enforceable turned it off.

ruff is now a pinned `lint` dependency group resolved from `uv.lock`. A group
rather than an extra because it is developer tooling that an installer of this
project should not acquire; an exact pin because a gate that answers
differently on different days is not a gate. The version is deliberately not
the Makefile's `SUBMODULE_RUFF`: that pin exists to reproduce upstream
micropython's CI and is not this repo's choice.

Checked the way the defect asks to be checked - by running `make lint` with
ruff absent from PATH, in an `env -i` shell holding only `uv`, `make` and
`git`, rather than by running it here where it was always going to work.

Run `31352461359` is the first green `firmware` run: Lint "All checks passed!",
then 361 passed, 20 skipped, 1 xfailed in 154.65s, on the first attempt with no
retry from the flake machinery.

## 361, where the same tree gives 362 here

The one test that runs locally and skips on a runner is
`test_s7_1_extension_suite_passes`, which shells out to the extension's
`npm test` and is marked skipped when npm or `extension/node_modules` is
missing. The `unix` job installs no node toolchain, so it skips there.

## Which means the extension's node tests had never run in CI either

`npm test` and `npm run test:host` are two different suites - the 98 node tests
under `out/test`, and the 10 that need a real VS Code. The extension-host job
ran only the second. The first reached CI only through the pytest case above,
in the job that skips it. So the node tests were covered by a job that does not
run them and a job that does not have them, which from a check list looks like
coverage twice over.

`npm test` is now its own step in the extension-host job, where `npm ci` has
already run. Confirmed executing on a runner: 98 tests, 98 pass, 0 fail
(run `31352747107`).

## The trigger listed what the jobs build, not what they are made of

The `push` paths filter named the submodules, `mbm.toml`, the workflow, the
scripts, `launcher/`, `tests/` and `extension/`. It did not name `Makefile`,
`pyproject.toml`, `uv.lock` or `src/`, which are respectively the commands CI
runs, the versions those resolve to, the lock behind them, and the sample
debuggees whose fixed line numbers the suite sets breakpoints at. A change to
any of them can break a job while touching nothing in the list. This commit's
own ruff pin is an instance: `Makefile` + `pyproject.toml` + `uv.lock`, and it
reached CI only because it was pushed alongside a `tests/` change. All four are
now in the list.

## The extension-host risk-register row is closed by its own logs

The row said the job had never executed a run, so its wiring was unverified and
its two cold steps - the VS Code download and the marketplace install of
`ms-python.debugpy` - had never been exercised anywhere.

It has run, and those two steps are exactly what the first green one did. Run
`31251545538`, job `93088575346`, 2026-08-08: cache miss on the key, "Downloaded
VS Code (07b4ff1883f94da91f6d698744fc7c3638b59720) into
`.vscode-test/vscode-linux-x64-1.132.0`", `Installing extensions...`, 10
passing, cache saved. Green again on 2026-08-08 15:38 and 2026-08-10 01:57 and
in both runs above, on cache hits.

The run that proved it was itself cancelled, which is why the row outlived the
evidence: `gh run list` reports the run's conclusion, and a cancelled run can
contain a job that finished and passed. Read the job, not the run.

## The failure mode that hides all of the above

Four runs between 2026-08-07 and 2026-08-08 never reached any job that tests
anything, because `compute version metadata` failed at checkout:

```
fatal: remote error: upload-pack: not our ref e143f3dbecf1a725c080bf9a31ea7cbb85e6dfb0
fatal: Fetched in submodule path 'micropython', but it did not contain <sha>.
```

Runs `31173463074`, `31231551263`, `31242832627`, `31249741775`, three
different `micropython` SHAs. The cause is ordering: the top repo's submodule
pointer was pushed while the commit it points at existed only locally. Every
job in the run is then skipped, and the run reads as a generic red rather than
as "nothing was checked".

The rule this gives: before pushing the top repo, confirm each submodule pin is
reachable on the fork (`git -C <sub> branch -r --contains <pin>`), not merely
committed. Both pins were checked that way before the pushes recorded here.

## What this says about the rest of the record

Every green claim in `ROADMAP.md` for the last four days rests on local runs.
That is not the same as false - the host suite is run here before each commit,
and the two runs above show it passing unchanged on a runner - but "CI is
green" was not among the things that had been established, and nothing in the
repo would have said so. A job that fails for an environmental reason reads,
from a distance, like a job that is merely flaky.
