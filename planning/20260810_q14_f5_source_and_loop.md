# Q14: reaching `--source` and `--loop` from an F5 launch

- Date: 2026-08-10
- Top repo HEAD: `5517ca13f6a9a623dc4f186e8162e98177922b12`
- `micropython`: `b6beb1a8043aa32329af304c8bf09aa7fb3006a3` (`mpy-debugpy`)
- `micropython-lib`: `3bd6c44697ecdd46da9944445a998adcee9479c1` (`mpy-debugpy`)

## The question

STORY-8.4's docs-vs-code pass recorded that `buildDebugArgs`
(`extension/src/command.ts`) emits `target`, `program`, `--port`, `--timeout`,
`--dap-log` and `--dap-log-file` and nothing else, and concluded that the two
flows EPIC-4 built - debugging a host directory the board has never held
(STORY-4.3) and re-running an edit with no upload (STORY-4.5) - are CLI-only.
The roadmap asked for the shape, not for a yes/no: launch.json keys mirroring
the flags, versus deriving `--source` from the file being debugged, and what
`--loop` does to the meaning of a client restart.

## One premise in the question is wrong

`--source` is not the only route into the mount-backed flow. `do_debug` reads
the flag *and* the resolved target's own key:

```python
if args.source is not None:
    ...
    source_root = os.path.realpath(args.source)
else:
    source_root = resolved.source if resolved is not None else None
```

(`micropython/tools/mpremote/mpremote/commands.py`, in `do_debug`.) Everything
downstream - the host-side existence and module-resolution checks, the
`mount_local`, the generated `pathMappings` - keys off `source_root`, not off
`args.source`. A `[target.<name>]` that declares `source` therefore already
mounts on an F5 launch today, because the extension passes the target name and
the CLI resolves the rest. Q13 put `source` in `mpdebug.toml` precisely so it
would not have to be repeated per invocation, and that decision is doing its
job through the extension as well as the CLI.

What is genuinely unreachable from F5 is narrower than the question stated:

1. A source root that is *not* in `mpdebug.toml` - an ad-hoc directory, or a
   per-launch-configuration override of the target's own key.
2. `--loop`, entirely. `mpdebug.toml` has no `loop` key (the recognised target
   keys are `kind`, `device`, `program`, `firmware`, `dap_device`, `source`),
   so there is no configuration route to it at all.

## Decision

Mirror both flags as launch-configuration properties, `source` (string) and
`loop` (boolean), passed straight through to argv by `buildDebugArgs`, and
omitted from argv when the property is absent.

Omission is the whole reason this does not duplicate `mpdebug.toml`. The
launch config carries a value only when the user wants to say something the
config file does not; when it says nothing, the CLI's existing precedence -
flag beats target key beats "no mount" - is what runs, unchanged. The
extension stays what `command.ts` already documents itself as: argv
construction with no policy of its own.

`source` is passed verbatim. VS Code has already expanded `${workspaceFolder}`
and friends by the time
`resolveDebugConfigurationWithSubstitutedVariables` sees the config, and a
relative path is resolved by `os.path.realpath` in the child, whose cwd the
extension sets from the launch config's `cwd` (default: the workspace folder).
So a relative `source` means "relative to the workspace folder", which is the
only reading a user would expect.

`--source` against a unix target is rejected by the CLI with a specific
message. The extension does not pre-check it. It could - it already resolves
a target's `kind` from `mpdebug.toml` for the `pathMappings` decision - but
that check would be a second copy of a rule the CLI owns, and a copy that
sees only configured targets, not literal connect strings. The CLI's error
reaches the user through the existing captured-output path.

## Rejected: deriving `--source` from the file being debugged

The alternative in the question was to infer a source root - from the active
editor, or from the target's `program` - and pass it without the user asking.
Rejected on two grounds, the second of which is the decisive one:

- It passes a flag the user never wrote, so a failure ("module X does not
  resolve under source root Y") names a directory that appears in no file the
  user can edit.
- `--source` *overrides* the target's `source`. Derivation is therefore not an
  additive convenience: on any project that already configured `source`, an
  inferred value would silently displace the configured one. The feature would
  break the case it is meant to serve.

## `--loop` and what a restart means

The two are not independent, and the roadmap was right to flag it. The target
launcher calls `debugpy.enable_restart()` only under `loop`
(`mpy_launch_debugpy.py`), and `enable_restart` is the only thing that sets
`restart_supported`, which is the only thing that makes the session advertise
`supportsRestartRequest` and accept `restart` rather than refusing it
(`debugpy/server/debug_session.py`, `debugpy/public_api.py`). So:

- without `loop`: `restart` is refused by the target; a restart from the
  client is whatever the client does when the adapter offers no restart
  request, and the mpremote child exits after the program's single run;
- with `loop`: `restart` is honoured on the target - the program's modules are
  evicted from `sys.modules` and re-imported - and the child stays attached
  between runs, waiting in `wait_for_restart`.

That difference is a property of the target and the CLI, not something the
extension chooses; exposing `loop` exposes it as-is. Not verified here:
whether VS Code's restart button, routed through the `ms-python.debugpy`
adapter that sits between the editor and this server, surfaces the target's
`supportsRestartRequest` as an in-place restart. That needs a real extension
host, and the extension-host job has never run (STORY-6.1 criterion 4). The
launch-config property is still correct either way - `--loop` is what the CLI
accepts - but the editor-side ergonomics of restart stay unproven, and the
`loop` property's description says what the flag does rather than promising
what the restart button will do.

## Not decided here

Whether `loop` should default to true for a device target. It is a behaviour
change to sessions that work today, and the evidence for it is exactly the
unverified restart-button question above.
