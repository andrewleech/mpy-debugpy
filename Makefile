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

.PHONY: bootstrap integrate firmware-unix mpy-cross test demo firmware-list firmware-verify clean

# One-shot setup: check out the recorded integration commits and the libraries
# the unix port needs. Checkout-only; rebuilding the integration branches from
# mbm.toml is the separate `integrate` target below.
bootstrap:
	git submodule update --init --recursive
	$(MAKE) -C $(UNIX_PORT) VARIANT=$(UNIX_VARIANT) submodules

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

mpy-cross:
	$(MAKE) -C $(MPY)/mpy-cross

# Build a debug-enabled unix firmware.
firmware-unix: mpy-cross
	$(MAKE) -C $(UNIX_PORT) VARIANT=$(UNIX_VARIANT) submodules
	$(MAKE) -C $(UNIX_PORT) VARIANT=$(UNIX_VARIANT) CFLAGS_EXTRA="$(DEBUG_CFLAGS)"
	@echo "Built: $(UNIX_PORT)/build-$(UNIX_VARIANT)/micropython"

# Run the host-side DAP test harness against a built unix firmware.
test:
	uv run --with pytest --with anyio --with debugpy python -m pytest tests/ -q

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
