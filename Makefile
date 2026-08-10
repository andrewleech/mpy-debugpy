# mpy-debugpy — integration + developer tooling
#
# The debug support is composed from in-flight branches via mbm (see mbm.toml).
# Firmware binaries are not stored in-repo; they are built here or fetched via
# launcher/firmware.py using firmware/firmware.toml.

MPY            := micropython
UNIX_PORT      := $(MPY)/ports/unix
UNIX_VARIANT   := standard
# The unix `standard` variant already enables settrace and local-name capture
# (MICROPY_PY_SYS_SETTRACE + MICROPY_PY_SYS_SETTRACE_LOCALNAMES) via #8767's
# mpconfigvariant.h, so no extra CFLAGS are needed for the unix build. Bytecode
# persistence of names into .mpy (MICROPY_PY_SYS_SETTRACE_LOCALNAMES_PERSIST) is
# intentionally off — it corrupts line numbers for all compiled code. For other
# ports, define MICROPY_PY_SYS_SETTRACE=1 and MICROPY_PY_SYS_SETTRACE_LOCALNAMES=1.
DEBUG_CFLAGS   :=

.PHONY: bootstrap install-hooks check-pins check-submodule-ci integrate \
        firmware-unix mpy-cross test lint lint-submodules lint-submodule-commits \
        demo firmware-list firmware-verify clean

# Versions pinned by both submodules' own CI jobs and .pre-commit-config.yaml;
# keep in step with them.
SUBMODULE_RUFF      := ruff@0.11.6
SUBMODULE_CODESPELL := codespell@2.4.1

# Commit range for lint-submodule-commits. The default is what CI uses, which
# only makes sense on a feature branch:
#   make lint-submodule-commits SUB=micropython-lib RANGE=upstream/master..add-debugpy-support
SUB   := micropython
RANGE := upstream/master..HEAD

# One-shot setup: check out the recorded integration commits and the libraries
# the unix port needs. Checkout-only; rebuilding the integration branches from
# mbm.toml is the separate `integrate` target below.
bootstrap: install-hooks
	git submodule update --init --recursive
	$(MAKE) -C $(UNIX_PORT) VARIANT=$(UNIX_VARIANT) submodules

# A core.hooksPath setting silently overrides .git/hooks, and a check that looks
# installed but never runs is exactly the failure this hook exists to prevent,
# so report that case instead of symlinking into the void.
install-hooks:
	@hooks=$$(git rev-parse --git-path hooks); \
	if git config --get core.hooksPath >/dev/null; then \
	  echo "core.hooksPath is set to $$(git config --get core.hooksPath);"; \
	  echo "install tools/pre-push-check.sh as pre-push there by hand."; \
	else \
	  mkdir -p "$$hooks"; \
	  ln -sf "$(CURDIR)/tools/pre-push-check.sh" "$$hooks/pre-push"; \
	  echo "Installed $$hooks/pre-push -> tools/pre-push-check.sh"; \
	fi

# A submodule pin that exists only in a local clone leaves the top repo looking
# correct and takes CI down at the submodule checkout, before any job runs.
# The pre-push hook runs this for each ref pushed; run it by hand to check HEAD.
check-pins:
	./tools/check-submodule-pins.sh

# Which gating jobs would a submodule PR run that have never run for the branch?
# A `pull_request` event uses the workflows from the merge with the base, so a
# job upstream added after a branch was cut runs against that branch on every PR
# while appearing nowhere in what the branch carries - and a green push build on
# the fork, which uses the pushed commit's workflows, cannot have covered it.
# Run before claiming a submodule branch is CI-green; the remedy for a hit is to
# rebase that branch onto upstream/master.
check-submodule-ci:
	uv run --no-project tools/check_submodule_ci.py

# Rebuild the integration branches from mbm.toml on top of the latest upstream
# master, into <integration>_update branches (mpy-debugpy_update). --local
# skips mbm's own push routing, which targets upstream, not the andrewleech
# fork; it also skips mbm's pre-fetch, so we fetch by hand first. Conflicts
# stop mbm with instructions: resolve, `git rebase --continue`, then re-run
# with `--resume`.
integrate:
	git -C $(MPY) fetch --all --quiet
	git -C micropython-lib fetch --all --quiet
	mbm rebase -s micropython --local
	mbm rebase -s micropython-lib --local
	@echo "Update branches rebuilt (mpy-debugpy_update in each submodule)."
	@echo "Verify them, then for each submodule:"
	@echo "  git -C <submodule> branch -f mpy-debugpy mpy-debugpy_update"
	@echo "  git -C <submodule> push andrewleech +mpy-debugpy:mpy-debugpy"
	@echo "mbm's own push routing targets upstream, not the fork; never run"
	@echo "mbm rebase without --local."
	@echo
	@echo "The rebuild moves the integration branch, not the feature branches"
	@echo "that face CI, so their gating-job state is reported but not fatal:"
	-@$(MAKE) --no-print-directory check-submodule-ci

mpy-cross:
	$(MAKE) -C $(MPY)/mpy-cross

# Build a debug-enabled unix firmware.
firmware-unix: mpy-cross
	$(MAKE) -C $(UNIX_PORT) VARIANT=$(UNIX_VARIANT) submodules
	$(MAKE) -C $(UNIX_PORT) VARIANT=$(UNIX_VARIANT) CFLAGS_EXTRA="$(DEBUG_CFLAGS)"
	@echo "Built: $(UNIX_PORT)/build-$(UNIX_VARIANT)/micropython"

# Run the host-side DAP test harness against a built unix firmware. Uses the
# project's `test` extra (pyproject.toml) so deps resolve from uv.lock rather
# than whatever `--with` would pull at run time.
test:
	uv run --extra test python -m pytest tests/ -q

# Lint only, no formatting: several tests break at a fixed line in the sample
# debuggees under src/, so reformatting them would move the breakpoints.
lint:
	uv run --group lint ruff check .

# `lint` excludes both submodules (pyproject.toml), so it says nothing about a
# change made inside one. Each submodule has its own config and its own CI jobs,
# and this reproduces the tree-content ones exactly - same pinned versions, same
# commands, run from the submodule so its config applies. Run it after editing a
# submodule; a red job there blocks the upstream PR.
#
# A submodule's CI is not fully described by the workflows in its own tree: an
# older feature branch can predate a job that still runs against it. `make
# check-submodule-ci` names those jobs; the PR's own check list
# (`gh api repos/<upstream>/commits/<sha>/check-runs`) is the final word. Neither
# this target nor that one alone is the definition of green.
lint-submodules:
	for sub in micropython micropython-lib; do \
	  echo "== $$sub =="; \
	  (cd $$sub && uvx $(SUBMODULE_RUFF) check . && uvx $(SUBMODULE_RUFF) format --diff . \
	    && uvx --with tomli $(SUBMODULE_CODESPELL)) || exit 1; \
	done

# The commit-message job, which reports as `build` in a PR's check list. Kept
# out of lint-submodules because it needs a range: an integration branch fails
# it by construction (it carries other people's commits and mbm-rebased
# duplicates), so only a feature branch range is meaningful.
lint-submodule-commits:
	cd $(SUB) && ./tools/verifygitlog.py -v $(RANGE) --no-merges

# Run the sample target under the debug launcher (unix). Attach VS Code to the
# host/port from the MPDBG-READY line.
demo:
	MICROPYPATH="$(CURDIR)/src:$(CURDIR)/micropython-lib/python-ecosys/debugpy" \
	$(UNIX_PORT)/build-$(UNIX_VARIANT)/micropython launcher/mpy_launch_debugpy.py target main

firmware-list:
	uv run launcher/firmware.py list

firmware-verify:
	uv run launcher/firmware.py verify

clean:
	-$(MAKE) -C $(UNIX_PORT) VARIANT=$(UNIX_VARIANT) clean
	rm -rf launcher/compiled
