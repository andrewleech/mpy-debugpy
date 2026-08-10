# Bench boot script for the mpy-debugpy hardware-in-loop runs: copy this to the
# board as `boot.py`, alongside a `_secrets.py` defining `SSID` and `PASSWORD`.
#
# Two things the debug flow needs and cannot do for itself, because both are
# settled before the first `mpremote` connection:
#
# - A second USB CDC interface, for DAP over serial. The firmware only builds
#   the capability in (`MICROPY_HW_USB_CDC_NUM (2)`); which interfaces are
#   actually enumerated is a boot-time choice, and the default is VCP+MSC.
#   `usb_mode` has to be called from boot.py: stm32 applies its default mode
#   after boot.py returns, and only when boot.py did not set one.
# - WiFi. STORY-6.2's boot script only reports the address it finds, so the
#   interface has to be up already.
#
# stm32-specific by design. A board of another port needs its own equivalent,
# and `MPY_DEBUG_HIL_DAP_DEVICE` should be left unset on one that has no second
# interface to enumerate.
import time

import pyb

pyb.usb_mode("VCP+VCP+MSC")

import network

import _secrets


def connect(timeout_ms=15000, attempts=3):
    # The first association after a reflash or a power cycle regularly fails
    # with STAT_CONNECT_FAIL while the AP is plainly in scan range, and taking
    # the interface down and up again clears it. A boot script that gave up
    # after one try left the board on the bench with no address, which reads
    # as a network-flow regression rather than as the bench needing a nudge.
    wlan = network.WLAN(network.STA_IF)
    for attempt in range(attempts):
        if attempt:
            wlan.active(False)
            time.sleep(1)
        wlan.active(True)
        if not wlan.isconnected():
            wlan.connect(_secrets.SSID, _secrets.PASSWORD)
            start = time.ticks_ms()
            while not wlan.isconnected():
                if time.ticks_diff(time.ticks_ms(), start) > timeout_ms:
                    print("wlan: connect timed out, status", wlan.status())
                    break
                time.sleep_ms(100)
        if wlan.isconnected():
            addr = wlan.ipconfig("addr4")[0]
            print("wlan:", addr)
            return addr
    return None


connect()
