"""Unit tests for the shared MPDBG-READY handshake parser (STORY-5.4).

Covers the single-source parser that serves both subprocess-stdout and
raw-REPL serial readers, with synthetic chunk sequences that don't require
a device or subprocess - just in-memory stream simulation.
"""

import json
import sys
from pathlib import Path

import pytest

# Add the mpremote package to sys.path to import the handshake parser.
_REPO_ROOT = Path(__file__).resolve().parents[1]
_mpremote_dir = str(_REPO_ROOT / "micropython" / "tools" / "mpremote")
if _mpremote_dir not in sys.path:
    sys.path.insert(0, _mpremote_dir)

from mpremote import mpdebug_handshake


class TestBasicParsing:
    """Single clean handshake line arrives intact in one chunk."""

    def test_simple_handshake_one_chunk(self):
        """A complete handshake in a single chunk parses and resolves correctly."""
        payload = {"host": "127.0.0.1", "port": 5678, "caps": {"settrace": True}}
        line = f"MPDBG-READY {json.dumps(payload)}\n"

        chunks = [line]
        chunk_iter = iter(chunks)

        result = mpdebug_handshake.read_handshake(
            lambda: next(chunk_iter, ""), timeout=1, control_kind="serial"
        )

        assert result["kind"] == "tcp"
        assert result["host"] == "127.0.0.1"
        assert result["port"] == 5678
        assert result["caps"] == {"settrace": True}
        assert result["raw_host"] == "127.0.0.1"

    def test_handshake_with_multiple_caps(self):
        """Caps dict with multiple boolean entries parses cleanly."""
        payload = {
            "host": "192.168.1.100",
            "port": 5678,
            "caps": {"settrace": True, "save_names": False, "set_local": True, "f_back": False},
        }
        line = f"MPDBG-READY {json.dumps(payload)}\n"

        chunks = [line]
        chunk_iter = iter(chunks)

        result = mpdebug_handshake.read_handshake(
            lambda: next(chunk_iter, ""), timeout=1, control_kind="serial"
        )

        assert result["caps"] == payload["caps"]


class TestChunkBoundaries:
    """Handshake split across chunk boundaries, including mid-line."""

    def test_split_at_prefix(self):
        """Handshake splits right at the end of `MPDBG-READY ` prefix."""
        payload = {"host": "10.0.0.1", "port": 5678, "caps": {}}
        line = f"MPDBG-READY {json.dumps(payload)}\n"

        chunks = [line[: len("MPDBG-READY ")], line[len("MPDBG-READY ") :]]
        chunk_iter = iter(chunks)

        result = mpdebug_handshake.read_handshake(
            lambda: next(chunk_iter, ""), timeout=1, control_kind="serial"
        )

        assert result["host"] == "10.0.0.1"

    def test_split_mid_json_value(self):
        """Handshake splits in the middle of a JSON string value."""
        payload = {"host": "split-host-name", "port": 1234, "caps": {}}
        line = f"MPDBG-READY {json.dumps(payload)}\n"

        # Split inside the host string
        mid = line.index("split-host") + len("split-host")
        chunks = [line[:mid], line[mid:]]
        chunk_iter = iter(chunks)

        result = mpdebug_handshake.read_handshake(
            lambda: next(chunk_iter, ""), timeout=1, control_kind="serial"
        )

        assert result["host"] == "split-host-name"

    def test_split_at_json_object_middle(self):
        """Handshake splits right between JSON fields."""
        payload = {"host": "1.2.3.4", "port": 9999, "caps": {"a": True}}
        line = f"MPDBG-READY {json.dumps(payload)}\n"

        # Split after the host field
        mid = line.index('"port"')
        chunks = [line[:mid], line[mid:]]
        chunk_iter = iter(chunks)

        result = mpdebug_handshake.read_handshake(
            lambda: next(chunk_iter, ""), timeout=1, control_kind="serial"
        )

        assert result["port"] == 9999

    def test_split_with_multiple_chunks(self):
        """Handshake delivered in many small chunks."""
        payload = {"host": "test.com", "port": 5000, "caps": {"x": True}}
        line = f"MPDBG-READY {json.dumps(payload)}\n"

        # Break into 5-character chunks
        chunks = [line[i : i + 5] for i in range(0, len(line), 5)]
        chunk_iter = iter(chunks)

        result = mpdebug_handshake.read_handshake(
            lambda: next(chunk_iter, ""), timeout=1, control_kind="serial"
        )

        assert result["host"] == "test.com"
        assert result["port"] == 5000


class TestResolutionMatrix:
    """Endpoint resolution rules: 0.0.0.0 handling and real addresses."""

    def test_unix_0_0_0_0_resolves_to_localhost(self):
        """Unix subprocess reporting 0.0.0.0 resolves to 127.0.0.1."""
        payload = {"host": "0.0.0.0", "port": 5678, "caps": {}}
        line = f"MPDBG-READY {json.dumps(payload)}\n"

        chunks = [line]
        chunk_iter = iter(chunks)

        result = mpdebug_handshake.read_handshake(
            lambda: next(chunk_iter, ""), timeout=1, control_kind="unix"
        )

        assert result["host"] == "127.0.0.1"
        assert result["raw_host"] == "0.0.0.0"  # diagnostic keeps the original

    def test_device_0_0_0_0_with_known_host(self):
        """Device reporting 0.0.0.0 with a known_host resolves to that host."""
        payload = {"host": "0.0.0.0", "port": 5000, "caps": {}}
        line = f"MPDBG-READY {json.dumps(payload)}\n"

        chunks = [line]
        chunk_iter = iter(chunks)

        result = mpdebug_handshake.read_handshake(
            lambda: next(chunk_iter, ""), timeout=1, control_kind="serial", known_host="192.168.1.42"
        )

        assert result["host"] == "192.168.1.42"
        assert result["raw_host"] == "0.0.0.0"

    def test_device_0_0_0_0_no_known_host_errors(self):
        """Device reporting 0.0.0.0 with no known_host is a hard error."""
        payload = {"host": "0.0.0.0", "port": 5000, "caps": {}}
        line = f"MPDBG-READY {json.dumps(payload)}\n"

        chunks = [line]
        chunk_iter = iter(chunks)

        with pytest.raises(
            mpdebug_handshake.HandshakeError,
            match="no network address.*port 5000",
        ):
            mpdebug_handshake.read_handshake(
                lambda: next(chunk_iter, ""), timeout=1, control_kind="serial"
            )

    def test_real_address_passes_through_verbatim(self):
        """A real device address is returned as-is, no resolution needed."""
        for addr in ["192.168.1.1", "10.0.0.50", "example.com", "2001:db8::1"]:
            payload = {"host": addr, "port": 5678, "caps": {}}
            line = f"MPDBG-READY {json.dumps(payload)}\n"

            chunks = [line]
            chunk_iter = iter(chunks)

            result = mpdebug_handshake.read_handshake(
                lambda: next(chunk_iter, ""), timeout=1, control_kind="serial"
            )

            assert result["host"] == addr
            assert result["raw_host"] == addr

    @pytest.mark.parametrize(
        "raw_host, control_kind, known_host",
        [
            ("0.0.0.0", "unix", None),
            ("0.0.0.0", "serial", "192.168.1.42"),
            ("192.168.1.1", "serial", None),
        ],
    )
    def test_no_resolved_path_yields_0_0_0_0(self, raw_host, control_kind, known_host):
        """Across the whole resolution matrix, no non-raising outcome ever hands
        back the 0.0.0.0 wildcard - only the raw_host diagnostic may carry it."""
        payload = {"host": raw_host, "port": 5678, "caps": {}}
        line = f"MPDBG-READY {json.dumps(payload)}\n"

        chunks = [line]
        chunk_iter = iter(chunks)

        result = mpdebug_handshake.read_handshake(
            lambda: next(chunk_iter, ""),
            timeout=1,
            control_kind=control_kind,
            known_host=known_host,
        )
        assert result["host"] != "0.0.0.0"
        assert result["raw_host"] == raw_host

    @pytest.mark.parametrize(
        "raw_host, control_kind, known_host",
        [
            ("0.0.0.0", "serial", "0.0.0.0"),  # a wildcard known_host is not an address either
            ("", "serial", None),  # an empty host is as unusable as 0.0.0.0 or ::
            ("::", "serial", None),  # the IPv6 wildcard
        ],
    )
    def test_wildcard_variants_never_resolve(self, raw_host, control_kind, known_host):
        """0.0.0.0 isn't the only unusable bind address: '' and '::' resolve
        the same way, and a wildcard known_host is as unusable as none at all."""
        payload = {"host": raw_host, "port": 5678, "caps": {}}
        line = f"MPDBG-READY {json.dumps(payload)}\n"

        chunks = [line]
        chunk_iter = iter(chunks)

        with pytest.raises(mpdebug_handshake.HandshakeError, match="no network address"):
            mpdebug_handshake.read_handshake(
                lambda: next(chunk_iter, ""),
                timeout=1,
                control_kind=control_kind,
                known_host=known_host,
            )

    def test_unknown_control_kind_raises(self):
        """A typo'd control_kind fails loudly rather than degrading into the
        no-known-address error, which would misreport the actual problem."""
        payload = {"host": "0.0.0.0", "port": 5678, "caps": {}}
        line = f"MPDBG-READY {json.dumps(payload)}\n"

        chunks = [line]
        chunk_iter = iter(chunks)

        with pytest.raises(mpdebug_handshake.HandshakeError, match="unknown control_kind"):
            mpdebug_handshake.read_handshake(
                lambda: next(chunk_iter, ""), timeout=1, control_kind="Unix"
            )


class TestDuplicateAndMissing:
    """Exactly-one-line contract: zero or duplicate lines are errors."""

    def test_no_handshake_times_out(self):
        """Timeout with no handshake line raises an error with captured tail."""
        chunks = ["some banner output\n", "more noise\n"]
        chunk_iter = iter(chunks)

        with pytest.raises(
            mpdebug_handshake.HandshakeError, match="timed out.*MPDBG-READY"
        ) as exc_info:
            mpdebug_handshake.read_handshake(
                lambda: next(chunk_iter, ""), timeout=0.01, control_kind="unix"
            )

        # Check that the captured tail is in the message
        assert "more noise" in str(exc_info.value)

    def test_two_handshake_lines_error(self):
        """Two MPDBG-READY lines in the same buffered batch is an error,
        with the captured tail in the message like the timeout/eof errors."""
        payload = {"host": "127.0.0.1", "port": 5678, "caps": {}}
        line = f"MPDBG-READY {json.dumps(payload)}\n"

        chunks = ["some banner output\n" + line + line]  # both lines in one chunk
        chunk_iter = iter(chunks)

        with pytest.raises(
            mpdebug_handshake.HandshakeError, match="expected exactly one.*got 2"
        ) as exc_info:
            mpdebug_handshake.read_handshake(
                lambda: next(chunk_iter, ""), timeout=1, control_kind="unix"
            )

        assert "banner output" in str(exc_info.value)

    def test_two_handshakes_in_separate_chunks_first_one_returns(self):
        """If the first MPDBG-READY arrives complete, a second arriving later is not detected.

        (The parser returns as soon as one is found; duplicates after that
        are not polled for. This is acceptable per the ticket - the emit
        side guarantees one and only one, so a test would have to
        intentionally break that guarantee to hit this case.)
        """
        payload = {"host": "127.0.0.1", "port": 5678, "caps": {}}
        line = f"MPDBG-READY {json.dumps(payload)}\n"

        chunks = [line, line]
        chunk_iter = iter(chunks)

        result = mpdebug_handshake.read_handshake(
            lambda: next(chunk_iter, ""), timeout=1, control_kind="unix"
        )

        assert result["host"] == "127.0.0.1"


class TestSchemaViolations:
    """Malformed payloads in the JSON."""

    def test_port_not_int(self):
        """Port as a string instead of int raises a schema error."""
        payload = {"host": "127.0.0.1", "port": "5678", "caps": {}}
        line = f"MPDBG-READY {json.dumps(payload)}\n"

        chunks = [line]
        chunk_iter = iter(chunks)

        with pytest.raises(mpdebug_handshake.HandshakeError, match="'port' is not an int"):
            mpdebug_handshake.read_handshake(
                lambda: next(chunk_iter, ""), timeout=1, control_kind="unix"
            )

    def test_port_is_bool_rejected(self):
        """Port as a boolean (which is a subclass of int in Python) is rejected."""
        payload = {"host": "127.0.0.1", "port": True, "caps": {}}
        line = f"MPDBG-READY {json.dumps(payload)}\n"

        chunks = [line]
        chunk_iter = iter(chunks)

        with pytest.raises(mpdebug_handshake.HandshakeError, match="'port' is not an int"):
            mpdebug_handshake.read_handshake(
                lambda: next(chunk_iter, ""), timeout=1, control_kind="unix"
            )

    def test_host_not_string(self):
        """Host as a number instead of string raises a schema error."""
        payload = {"host": 127, "port": 5678, "caps": {}}
        line = f"MPDBG-READY {json.dumps(payload)}\n"

        chunks = [line]
        chunk_iter = iter(chunks)

        with pytest.raises(mpdebug_handshake.HandshakeError, match="'host' is not a string"):
            mpdebug_handshake.read_handshake(
                lambda: next(chunk_iter, ""), timeout=1, control_kind="unix"
            )

    def test_caps_not_dict(self):
        """Caps as a list instead of dict raises a schema error."""
        payload = {"host": "127.0.0.1", "port": 5678, "caps": [True]}
        line = f"MPDBG-READY {json.dumps(payload)}\n"

        chunks = [line]
        chunk_iter = iter(chunks)

        with pytest.raises(mpdebug_handshake.HandshakeError, match="'caps' is not a table of booleans"):
            mpdebug_handshake.read_handshake(
                lambda: next(chunk_iter, ""), timeout=1, control_kind="unix"
            )

    def test_caps_dict_with_non_bool_value(self):
        """Caps dict with a non-boolean value raises a schema error."""
        payload = {"host": "127.0.0.1", "port": 5678, "caps": {"settrace": "yes"}}
        line = f"MPDBG-READY {json.dumps(payload)}\n"

        chunks = [line]
        chunk_iter = iter(chunks)

        with pytest.raises(mpdebug_handshake.HandshakeError, match="'caps' is not a table of booleans"):
            mpdebug_handshake.read_handshake(
                lambda: next(chunk_iter, ""), timeout=1, control_kind="unix"
            )

    def test_missing_host_key(self):
        """Payload missing the 'host' key raises a KeyError."""
        payload = {"port": 5678, "caps": {}}
        line = f"MPDBG-READY {json.dumps(payload)}\n"

        chunks = [line]
        chunk_iter = iter(chunks)

        with pytest.raises(mpdebug_handshake.HandshakeError, match="missing key"):
            mpdebug_handshake.read_handshake(
                lambda: next(chunk_iter, ""), timeout=1, control_kind="unix"
            )

    def test_missing_port_key(self):
        """Payload missing the 'port' key raises a KeyError."""
        payload = {"host": "127.0.0.1", "caps": {}}
        line = f"MPDBG-READY {json.dumps(payload)}\n"

        chunks = [line]
        chunk_iter = iter(chunks)

        with pytest.raises(mpdebug_handshake.HandshakeError, match="missing key"):
            mpdebug_handshake.read_handshake(
                lambda: next(chunk_iter, ""), timeout=1, control_kind="unix"
            )

    def test_missing_caps_key(self):
        """Payload missing the 'caps' key raises a KeyError."""
        payload = {"host": "127.0.0.1", "port": 5678}
        line = f"MPDBG-READY {json.dumps(payload)}\n"

        chunks = [line]
        chunk_iter = iter(chunks)

        with pytest.raises(mpdebug_handshake.HandshakeError, match="missing key"):
            mpdebug_handshake.read_handshake(
                lambda: next(chunk_iter, ""), timeout=1, control_kind="unix"
            )

    def test_payload_not_json_object(self):
        """Payload that parses as a list instead of a dict raises an error."""
        line = "MPDBG-READY [1, 2, 3]\n"

        chunks = [line]
        chunk_iter = iter(chunks)

        with pytest.raises(mpdebug_handshake.HandshakeError, match="expected a JSON object"):
            mpdebug_handshake.read_handshake(
                lambda: next(chunk_iter, ""), timeout=1, control_kind="unix"
            )

    def test_invalid_json(self):
        """Malformed JSON in the handshake line raises a parse error."""
        line = "MPDBG-READY {not valid json}\n"

        chunks = [line]
        chunk_iter = iter(chunks)

        with pytest.raises(mpdebug_handshake.HandshakeError, match="malformed.*line from the device"):
            mpdebug_handshake.read_handshake(
                lambda: next(chunk_iter, ""), timeout=1, control_kind="unix"
            )


class TestAdversarial:
    """Edge cases: prefix matching, trailing bytes, and decoder robustness."""

    def test_mpdbg_ready_as_substring_not_at_line_start(self):
        """MPDBG-READY appearing as a substring mid-line does not match.

        The decoy carries a different host/port than the real line, so a
        parser that matched the decoy (substring match) rather than the real
        line (start-of-line match) would return the decoy's values, and the
        decoy would never reach `on_line` - both are asserted below to catch
        that regression, not just that some line was parsed.
        """
        decoy_payload = {"host": "203.0.113.9", "port": 1, "caps": {}}
        real_payload = {"host": "127.0.0.1", "port": 5678, "caps": {}}
        real_line = f"MPDBG-READY {json.dumps(real_payload)}\n"

        # Has the prefix as a substring but not at the start.
        fake_line = f"prefix MPDBG-READY {json.dumps(decoy_payload)}\n"
        chunks = [fake_line, real_line]
        chunk_iter = iter(chunks)
        passed_lines = []

        result = mpdebug_handshake.read_handshake(
            lambda: next(chunk_iter, ""),
            timeout=1,
            control_kind="unix",
            on_line=passed_lines.append,
        )

        assert result["host"] == "127.0.0.1"
        assert result["port"] == 5678
        assert passed_lines == [fake_line]  # the decoy was treated as banner text

    def test_trailing_carriage_return_and_echo_bytes(self):
        """JSON followed by trailing bytes (\\r, echo chars) before newline is parsed OK.

        raw_decode stops at the closing brace, so extraneous bytes don't
        break the JSON decode, matching the raw-REPL serial scenario where
        the device echoes input or leaves prompt bytes.
        """
        payload = {"host": "127.0.0.1", "port": 5678, "caps": {}}
        json_str = json.dumps(payload)
        line = f"MPDBG-READY {json_str}\r\x00extra\n"  # \r, null, junk before newline

        chunks = [line]
        chunk_iter = iter(chunks)

        result = mpdebug_handshake.read_handshake(
            lambda: next(chunk_iter, ""), timeout=1, control_kind="unix"
        )

        assert result["host"] == "127.0.0.1"

    def test_banner_lines_before_handshake(self):
        """Human-readable banner lines before the handshake are ignored."""
        payload = {"host": "127.0.0.1", "port": 5678, "caps": {}}
        handshake_line = f"MPDBG-READY {json.dumps(payload)}\n"

        banner = "Debugpy listening on 127.0.0.1:5678\nWaiting for debugger attach...\n"
        chunks = [banner + handshake_line]
        chunk_iter = iter(chunks)

        result = mpdebug_handshake.read_handshake(
            lambda: next(chunk_iter, ""), timeout=1, control_kind="unix"
        )

        assert result["host"] == "127.0.0.1"

    def test_on_line_callback_receives_banner_lines(self):
        """The on_line callback is invoked for non-handshake lines."""
        payload = {"host": "127.0.0.1", "port": 5678, "caps": {}}
        handshake_line = f"MPDBG-READY {json.dumps(payload)}\n"

        banner_lines = ["Banner line 1\n", "Banner line 2\n"]
        chunks = [banner_lines[0] + banner_lines[1] + handshake_line]
        chunk_iter = iter(chunks)

        captured = []

        def on_line(line):
            captured.append(line)

        mpdebug_handshake.read_handshake(
            lambda: next(chunk_iter, ""),
            timeout=1,
            control_kind="unix",
            on_line=on_line,
        )

        assert captured == banner_lines

    def test_eof_marker_ends_wait_immediately(self):
        """An eof marker ends the wait immediately instead of timing out."""
        chunks = ["some output\n", "\x04"]  # \x04 is raw-REPL end-of-output marker
        chunk_iter = iter(chunks)

        with pytest.raises(mpdebug_handshake.HandshakeError, match="device exited"):
            mpdebug_handshake.read_handshake(
                lambda: next(chunk_iter, ""),
                timeout=10,  # Would timeout if eof didn't cut it short
                control_kind="unix",
                eof="\x04",
            )

    def test_eof_callback_appends_to_error(self):
        """The on_eof callback can append diagnostic data to the error message."""
        chunks = ["output 1\n", "output 2\n", "\x04extra bytes"]
        chunk_iter = iter(chunks)

        def on_eof(rest):
            return f"; additional context: got {len(rest)} extra bytes"

        with pytest.raises(mpdebug_handshake.HandshakeError) as exc_info:
            mpdebug_handshake.read_handshake(
                lambda: next(chunk_iter, ""),
                timeout=1,
                control_kind="unix",
                eof="\x04",
                on_eof=on_eof,
            )

        assert "additional context: got 11 extra bytes" in str(exc_info.value)

    def test_eof_glued_to_following_line_still_triggers_immediately(self):
        """The eof marker is caught even when a chunk carries it and the line after
        it together - the real framing over serial, since a `read_until(1, b"\\n")`
        poll returns through the first newline regardless of what precedes it, so
        the raw-REPL `\\x04` marker always arrives stuck to the exception line that
        follows rather than as a chunk of its own. `on_eof` must see that trailing
        text, and it must not be echoed as an ordinary banner line first.
        """
        chunks = ["\x04Traceback (most recent call last):\r\n"]
        chunk_iter = iter(chunks)
        seen_lines = []

        with pytest.raises(mpdebug_handshake.HandshakeError, match="device exited") as exc_info:
            mpdebug_handshake.read_handshake(
                lambda: next(chunk_iter, ""),
                timeout=10,  # would time out if the marker weren't caught in the first chunk
                control_kind="unix",
                on_line=seen_lines.append,
                eof="\x04",
                on_eof=lambda rest: f"; device error: {rest.strip()}",
            )

        assert seen_lines == [], f"traceback must not be echoed as ordinary lines: {seen_lines}"
        assert "device error: Traceback (most recent call last):" in str(exc_info.value)

    def test_initial_buffer_seed(self):
        """The initial parameter seeds the buffer with pre-drained text."""
        payload = {"host": "127.0.0.1", "port": 5678, "caps": {}}
        full_line = f"MPDBG-READY {json.dumps(payload)}\n"

        # Split the line: first part is "initial" (already read), rest comes later
        split_point = full_line.index("{")
        initial_text = full_line[:split_point]
        remaining_text = full_line[split_point:]

        chunks = [remaining_text]
        chunk_iter = iter(chunks)

        result = mpdebug_handshake.read_handshake(
            lambda: next(chunk_iter, ""),
            timeout=1,
            control_kind="unix",
            initial=initial_text,
        )

        assert result["host"] == "127.0.0.1"

    def test_payload_with_extra_fields_allowed(self):
        """Payload with extra unknown fields is accepted (forward compatibility)."""
        payload = {
            "host": "127.0.0.1",
            "port": 5678,
            "caps": {},
            "future_field": "ignored",
            "another_field": 42,
        }
        line = f"MPDBG-READY {json.dumps(payload)}\n"

        chunks = [line]
        chunk_iter = iter(chunks)

        result = mpdebug_handshake.read_handshake(
            lambda: next(chunk_iter, ""), timeout=1, control_kind="unix"
        )

        assert result["host"] == "127.0.0.1"


class TestSubprocessAndSerialSources:
    """Integration-like tests showing the parser works with different source patterns."""

    def test_subprocess_stdout_source(self):
        """Simulates a non-blocking subprocess stdout reader."""
        payload = {"host": "127.0.0.1", "port": 5678, "caps": {"settrace": True}}
        handshake_line = f"MPDBG-READY {json.dumps(payload)}\n"

        # Simulate a non-blocking read that drains chunks
        chunks = [handshake_line]
        chunk_iter = iter(chunks)

        def read_chunk():
            try:
                return next(chunk_iter)
            except StopIteration:
                return ""

        result = mpdebug_handshake.read_handshake(
            read_chunk,
            timeout=1,
            control_kind="unix",
        )

        assert result["kind"] == "tcp"
        assert result["port"] == 5678

    def test_raw_repl_source_with_echo_and_exception_marker(self):
        """Simulates a raw-REPL reader with device echo and exception handling."""
        payload = {"host": "192.168.1.1", "port": 5000, "caps": {}}
        handshake_line = f"MPDBG-READY {json.dumps(payload)}\n"

        # Simulate raw-REPL behavior: echo of input, then output
        chunks = [
            "import debugpy\n",  # echo of the import statement
            "debugpy.listen(...)\n",  # echo/output
            handshake_line,  # the actual handshake
            "\x04",  # raw-REPL end-of-output
        ]
        chunk_iter = iter(chunks)

        def read_chunk():
            try:
                return next(chunk_iter)
            except StopIteration:
                return ""

        result = mpdebug_handshake.read_handshake(
            read_chunk,
            timeout=1,
            control_kind="serial",
            eof="\x04",
        )

        assert result["host"] == "192.168.1.1"
        assert result["port"] == 5000
