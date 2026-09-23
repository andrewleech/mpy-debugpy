# PR / MR drafts

- Date: 2026-09-23
- Top repo: `c113c399b5` · `micropython`: `bfdb690267` · `micropython-lib`: `788b2a5568`
- upstream/master at time of writing: micropython `09f5bb4475`

One file per branch that should eventually become an upstream PR, plus one per already-open upstream PR carrying a drafted follow-up comment (those descriptions are not rewritten; see the memory note on appending rather than rewriting). Frontmatter follows the `draft-pr` skill's schema: `upstream_repo`, `upstream_base`, `local_branch`, `status`, `title`, and where relevant `pushed_branch`, `upstream_pr`, `head`, `depends_on`, `relationship`, `todo`.

Per the `draft-pr` skill and STORY-8.3, new PRs are raised on the fork first (targeting `andrewleech/<repo>` `master`) for review, and nothing is opened upstream until the user has signed off on the fork PR.

## New PRs

| draft | repo | depends on | state |
|---|---|---|---|
| [mpremote_transport_fixes](mpremote_transport_fixes.md) | micropython | - | local only; rebase clean |
| [mpremote_debug_command](mpremote_debug_command.md) | micropython | transport_fixes; #1022 to be usable | local only; docs conflict on rebase; soft-reset decision after #17485 |
| [mpremote_dap_repl](mpremote_dap_repl.md) | micropython | debug_command | local only; the branch mbm composes |
| [unix_stdin_read_error](unix_stdin_read_error.md) | micropython | - | pushed; fix not yet measured after |
| [settrace_loop_line_events](settrace_loop_line_events.md) | micropython | - | pushed |
| [mpremote_close_lost_device](mpremote_close_lost_device.md) | micropython | - | pushed |
| [mpremote_debugpy_install](mpremote_debugpy_install.md) | micropython | #18436; #1022 | pushed; broken without #18436; upstream fit questionable |
| [local_names_implementation](local_names_implementation.md) | micropython | #8767 | pushed; needs cleanup (PERSIST option, debug knobs, history) |

The three `mpremote_` debug branches are a stack (A ⊂ B ⊂ C) and are raised as three stacked PRs in that order. They supersede fork PR andrewleech/micropython#51.

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
