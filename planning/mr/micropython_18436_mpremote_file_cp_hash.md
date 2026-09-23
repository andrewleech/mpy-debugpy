---
upstream_repo: micropython/micropython
upstream_base: master
local_branch: mpremote_file_cp_hash
status: PR open upstream, conflicts with master; has a blocking bug (see todo); rebased branch d940a381c0 local only; follow-up comment drafted, not posted
title: "tools/mpremote: Add streaming hash verification to file transfers"
upstream_pr: https://github.com/micropython/micropython/pull/18436
head: d940a381c0
relationship: |
  mpremote_debugpy_install calls fs_writefile(verify_hash=True) from this PR.
  ~/ampremote carries a merged fs_writefile that supports both verify_hash and
  the `encoding` parameter of #18785; if #18785 lands first this needs the same
  merge.
todo:
  - >-
    PR head 80b2a1aeb6 conflicts with master in transport.py (upstream now quotes paths with _quote_path()). d940a381c0 is the same change rebased onto 06bcfd5b74 with the open() calls switched to _quote_path(); range-diff shows nothing else changed. That base is itself 145 behind master now; rebase again before pushing.
  - >-
    BLOCKING: no fallback when the device lacks hashlib.sha256. The thread reply (2025-11-19) says the feature is gated on the same check mpremote already uses, but it isn't: `check_hash` in do_filesystem_cp is only the inverse of `--force`, and fs_writefile/fs_readfile with verify_hash=True exec `import hashlib\nh=hashlib.sha256()` unconditionally. On a device without sha256 that raises TransportExecError, so a plain `mpremote cp` fails where it used to copy. fs_hashfile has the right pattern (catch the exec error, fall back). Fix, add a test (the unix port can be built without sha256), then post the comment. Not reproduced on hardware yet; read from the code at d940a381c0.
  - >-
    Title is missing its trailing period.
---

## Follow-up comment

Rebased onto master. The only change is that the `open()` calls in `fs_readfile` / `fs_writefile` use `_quote_path()` now, to match the path quoting that landed on master since this was opened.

Also fixed the case Jos asked about: I'd said this was gated on the device having `hashlib.sha256`, but it wasn't, and a device without it failed the copy. It now probes for it when setting up the transfer and copies unverified if it's missing, same as `fs_hashfile` does. <!-- TODO: only true once the fix in the todo above is made; name the test added. -->
