"""Debuggee for the extension-host suite's child-termination test.

Unlike src/target.py's main(), this one never returns once resumed: the
child-termination test needs a debuggee that can only be ended by an
explicit kill, never by finishing on its own, so that a session-end that
fails to kill its mpremote child is observable within a bounded wait.
"""


def main():
    running = True
    while running:
        running = running
