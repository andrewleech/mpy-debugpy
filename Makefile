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
# the unix port needs. This does NOT run `mbm rebase` — the integration branches
# are composed by hand (merge + cherry-pick onto current master, see mbm.toml),
# not yet reconstructable via mbm automation.
bootstrap:
	git submodule update --init --recursive
	$(MAKE) -C $(UNIX_PORT) VARIANT=$(UNIX_VARIANT) submodules

# Reconstruct the integration branches from mbm.toml on top of upstream master.
# NOT part of bootstrap and not yet wired: the branches are currently hand-composed
# (merge + cherry-pick). Expressing them as mbm add-pr composition is the
# upstreaming epic (see planning/ROADMAP.md).
integrate:
	@echo "mbm auto-composition not wired yet; branches are hand-composed (see mbm.toml)."
	@echo "See planning/ROADMAP.md upstreaming epic."

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
