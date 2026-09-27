# PR / MR drafts

- Date: 2026-09-23
- Top repo: `c113c399b5` · `micropython`: `bfdb690267` · `micropython-lib`: `788b2a5568`
- upstream/master at time of writing: micropython `09f5bb4475`

One file per branch that should eventually become an upstream PR, plus one per already-open upstream PR carrying a drafted follow-up comment (those descriptions are not rewritten; see the memory note on appending rather than rewriting). Frontmatter follows the `draft-pr` skill's schema: `upstream_repo`, `upstream_base`, `local_branch`, `status`, `title`, and where relevant `pushed_branch`, `upstream_pr`, `head`, `depends_on`, `relationship`, `todo`.

New work goes through draft PRs on `andrewleech/micropython` for self-review before any separate upstream PR. Independent branches use a `review/mpy-debugpy-<branch>` base pinned at their original upstream base, keeping the fork PR diff limited to the branch's own commits. The debug stack uses the preceding feature branch as its base. These fork PRs are public drafts, not private GitHub PRs.

## New PRs

| draft | repo | depends on | state |
|---|---|---|---|
| [mpremote_transport_fixes](mpremote_transport_fixes.md) | micropython | - | [fork draft #69](https://github.com/andrewleech/micropython/pull/69) |
| [mpremote_debug_command](mpremote_debug_command.md) | micropython | transport_fixes; #1022 to be usable | [fork draft #70](https://github.com/andrewleech/micropython/pull/70), stacked on #69 |
| [mpremote_dap_repl](mpremote_dap_repl.md) | micropython | debug_command | [fork draft #71](https://github.com/andrewleech/micropython/pull/71), stacked on #70 |
| [unix_stdin_read_error](unix_stdin_read_error.md) | micropython | - | [fork draft #72](https://github.com/andrewleech/micropython/pull/72); post-fix syscall count still open |
| [settrace_loop_line_events](settrace_loop_line_events.md) | micropython | - | [fork draft #73](https://github.com/andrewleech/micropython/pull/73) |
| [mpremote_close_lost_device](mpremote_close_lost_device.md) | micropython | - | [fork draft #74](https://github.com/andrewleech/micropython/pull/74) |
| [mpremote_debugpy_install](mpremote_debugpy_install.md) | micropython | #18436; #1022 | [fork draft #75](https://github.com/andrewleech/micropython/pull/75); broken without #18436, upstream fit unresolved |
| [local_names_implementation](local_names_implementation.md) | micropython | #8767 | [existing fork PR #5](https://github.com/andrewleech/micropython/pull/5); needs cleanup |

The three `mpremote_` debug branches are a stack (A ⊂ B ⊂ C) and are raised as three stacked PRs in that order. They supersede fork PR andrewleech/micropython#51.

The fork drafts and their 21 feature commits were rewritten for the working style on 2026-09-27. The integration was rebuilt at `d7fb7f3795`; its tree is byte-identical to the previous pin `7c8dd9c90e`. The published firmware still records that original source commit because those are the binaries it was built from. The old pin remains reachable via its `mpy-debugpy-pin-7c8dd9c90e` tag.

## Open upstream PRs (follow-up comments)

| draft | PR | state |
|---|---|---|
| [micropython_8767_pdb_support](micropython_8767_pdb_support.md) | micropython#8767 | a764b6cd8c pushed with no explanation; comment ready |
| [micropython_18436_mpremote_file_cp_hash](micropython_18436_mpremote_file_cp_hash.md) | micropython#18436 | conflicts with master; blocking sha256-fallback bug |
| [micropython-lib_1022_add-debugpy-support](micropython-lib_1022_add-debugpy-support.md) | micropython-lib#1022 | 21 foreign commits + a merge commit on the PR; needs rebase and fold before the comment |

## Not for upstream

`debug_board_flags` enables `MICROPY_PY_SYS_SETTRACE` and `..._LOCALNAMES` on RPI_PICO_W, PYBD_SF6 and ESP32_GENERIC so this project's released debug firmware has them. Upstream won't enable settrace on stock board definitions (it costs size and speed on every build), so it stays an integration-only branch. If upstream wants anything here it's a `DEBUG` board variant, which is a different PR.

## Cross-cutting

- **#17485 (resume by default) merged 2026-09-02.** mpremote no longer soft-resets before the first command. Affects `mpremote_debug_command` (a second session finds the previous target and debugpy imported) and `mpremote_debugpy_install` (its "a separate invocation soft-resets" reasoning no longer holds).
- **Author identity** is mixed across branches: `andrew@alelec.net` on the debug stack, unix_stdin_read_error, settrace_loop_line_events and close_lost_device; `andrew.leech@planetinnovation.com.au` on #8767, local_names, debugpy_install, #18436, debug_board_flags and part of #1022. Each PR is consistent within itself except #1022.
- **No branch here has a clean `/mpy-rules:review`.** The only review (2026-08-21) was of the pre-split `mpremote_debug`.
