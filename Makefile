# mpy-debugpy — integration + developer tooling
#
# The debug support is composed from in-flight branches via mbm (see mbm.toml).
# Firmware binaries are not stored in-repo; they are built here or fetched via
# launcher/firmware.py using firmware/firmware.toml.

MPY            := micropython
UNIX_PORT      := $(MPY)/ports/unix
UNIX_VARIANT   := standard
# settrace + local-name preservation; adjust per what the composed branches expose
DEBUG_CFLAGS   := -DMICROPY_PY_SYS_SETTRACE=1 -DMICROPY_PY_SYS_SETTRACE_SAVE_NAMES=1

.PHONY: bootstrap integrate firmware-unix mpy-cross test demo firmware-list firmware-verify clean

# One-shot setup: check out the recorded integration commits and the libraries
# the unix port needs. This does NOT run `mbm rebase` — the integration branches
# are currently vendored (based directly on the upstream debug-support fork
# branches, see mbm.toml), not yet reconstructable from upstream master.
bootstrap:
	git submodule update --init --recursive
	$(MAKE) -C $(UNIX_PORT) VARIANT=$(UNIX_VARIANT) submodules

# Reconstruct the integration branches from mbm.toml on top of upstream master.
# NOT part of bootstrap: the current branches are vendored fork integrations and
# a rebase-from-master would discard them. Wiring clean mbm composition is the
# upstreaming epic (see planning/ROADMAP.md).
integrate:
	@echo "Refusing to auto-rebase: integration branches are vendored (see Makefile/mbm.toml)."
	@echo "Run 'mbm rebase -s <submodule>' manually once the composition is expressed as PRs."

mpy-cross:
	$(MAKE) -C $(MPY)/mpy-cross

# Build a debug-enabled unix firmware.
firmware-unix: mpy-cross
	$(MAKE) -C $(UNIX_PORT) VARIANT=$(UNIX_VARIANT) submodules
	$(MAKE) -C $(UNIX_PORT) VARIANT=$(UNIX_VARIANT) CFLAGS_EXTRA="$(DEBUG_CFLAGS)"
	@echo "Built: $(UNIX_PORT)/build-$(UNIX_VARIANT)/micropython"

# Run the host-side DAP test harness against a built unix firmware.
test:
	uv run --with pytest python -m pytest tests/ -q

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
