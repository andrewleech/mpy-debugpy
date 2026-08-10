# Bench boot script for the single-UART arrangement: copy this to the board as
# `boot.py`, replacing `board_boot.py`, and reset.
#
# The mirror of `board_boot.py`, and the same bench board. That script arranges
# the two things the second-CDC and network scenarios need; this one arranges
# their absence, which is what the single-UART scenarios are about:
#
# - No `pyb.usb_mode()` call, so stm32 applies its own default after boot.py
#   returns and the board enumerates one VCP. Deleting the line is not enough
#   on a board that already booted with two - the mode is chosen at boot, so
#   the board has to come up again with this file in place.
# - No WiFi, so the board has no address for the network transport to report
#   and the DAP channel has nowhere to go but the stream carrying the REPL.
#
# Deliberately empty of anything else. A board as it ships is the arrangement
# under test, and every line here would be a way for the bench to differ from
# one.
