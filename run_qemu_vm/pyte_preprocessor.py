#!/usr/bin/env python3
"""
Stateful preprocessor for filtering CSI sequences before pyte processing.

Filters out CSI sequences with intermediate characters (0x20-0x2F) that pyte's
parser doesn't handle correctly, causing characters like 'p' to leak through
as literal text. Also filters extreme cursor position movements.

The preprocessor uses a finite state machine to handle sequences that may be
split across multiple PTY read chunks.
"""

from .logging_utils import debug_log


# FSM States
STATE_NORMAL = 0
STATE_ESC_SEEN = 1       # Saw ESC (0x1b)
STATE_CSI_STARTED = 2    # Saw ESC[
STATE_CSI_INTERMEDIATE = 3  # Saw intermediate char (0x20-0x2F) in CSI


class PytePreprocessor:
    """
    Stateful filter for CSI sequences that pyte cannot handle.

    Removes:
    - CSI sequences with intermediate characters (e.g., ESC[!p for DECSTR)
    - Extreme cursor position movements beyond screen dimensions

    Uses a finite state machine with internal buffer to handle sequences
    split across multiple input chunks.
    """

    MAX_BUFFER_SIZE = 64

    def __init__(self, screen, debug_file=None):
        """
        Initialize the preprocessor.

        Args:
            screen: A pyte.Screen instance to query for dimensions.
            debug_file: Optional file handle for debug logging.
        """
        self._screen = screen
        self._debug_file = debug_file
        self._state = STATE_NORMAL
        self._buffer = bytearray()

    def _reset_state(self):
        """Reset FSM to normal state with empty buffer."""
        self._state = STATE_NORMAL
        self._buffer.clear()

    def _is_csi_final_byte(self, byte):
        """Check if byte is a CSI final byte (0x40-0x7E)."""
        return 0x40 <= byte <= 0x7E

    def _is_csi_intermediate_byte(self, byte):
        """Check if byte is a CSI intermediate byte (0x20-0x2F)."""
        return 0x20 <= byte <= 0x2F

    def _is_csi_parameter_byte(self, byte):
        """Check if byte is a CSI parameter byte (0x30-0x3F)."""
        return 0x30 <= byte <= 0x3F

    def _parse_cursor_position(self, params_bytes):
        """
        Parse cursor position parameters from CSI sequence.

        Args:
            params_bytes: The parameter bytes between ESC[ and H.

        Returns:
            Tuple (row, col) or None if parsing fails.
        """
        try:
            params_str = params_bytes.decode('ascii', errors='replace')
            if ';' in params_str:
                parts = params_str.split(';')
                if len(parts) == 2:
                    row = int(parts[0]) if parts[0] else 1
                    col = int(parts[1]) if parts[1] else 1
                    return (row, col)
            elif params_str:
                return (int(params_str), 1)
            return (1, 1)
        except (ValueError, IndexError):
            return None

    def _should_filter_cursor_position(self, params_bytes):
        """
        Check if a cursor position sequence should be filtered.

        Filters cursor positions that exceed screen dimensions.

        Args:
            params_bytes: The parameter bytes between ESC[ and H.

        Returns:
            True if the sequence should be filtered, False otherwise.
        """
        pos = self._parse_cursor_position(params_bytes)
        if pos is None:
            return False

        row, col = pos
        max_rows = self._screen.lines
        max_cols = self._screen.columns

        # Filter if either coordinate exceeds screen dimensions
        if row > max_rows or col > max_cols:
            debug_log(
                self._debug_file,
                f"PytePreprocessor: filtering extreme cursor position "
                f"({row}, {col}) exceeds screen ({max_rows}, {max_cols})"
            )
            return True
        return False

    def filter(self, data):
        """
        Filter CSI sequences with intermediate characters from input data.

        Args:
            data: Raw bytes from PTY.

        Returns:
            Filtered bytes safe for pyte processing.
        """
        output = bytearray()

        for byte in data:
            if self._state == STATE_NORMAL:
                if byte == 0x1b:  # ESC
                    self._state = STATE_ESC_SEEN
                    self._buffer.append(byte)
                else:
                    output.append(byte)

            elif self._state == STATE_ESC_SEEN:
                self._buffer.append(byte)
                if byte == 0x5b:  # '['
                    self._state = STATE_CSI_STARTED
                else:
                    # Not a CSI sequence, emit buffer and reset
                    output.extend(self._buffer)
                    self._reset_state()

            elif self._state == STATE_CSI_STARTED:
                self._buffer.append(byte)

                if self._is_csi_intermediate_byte(byte):
                    # Intermediate character found - this sequence will be filtered
                    self._state = STATE_CSI_INTERMEDIATE

                elif self._is_csi_parameter_byte(byte):
                    # Parameter byte, stay in CSI_STARTED
                    pass

                elif self._is_csi_final_byte(byte):
                    # Final byte reached - check for extreme cursor position
                    if byte == 0x48:  # 'H' - cursor position
                        # Extract params (everything between ESC[ and H)
                        params = bytes(self._buffer[2:-1])
                        if self._should_filter_cursor_position(params):
                            # Filter this sequence
                            self._reset_state()
                        else:
                            # Emit the complete sequence
                            output.extend(self._buffer)
                            self._reset_state()
                    else:
                        # Other CSI sequence without intermediate, emit it
                        output.extend(self._buffer)
                        self._reset_state()

                else:
                    # Unexpected byte, emit buffer and reset
                    output.extend(self._buffer)
                    self._reset_state()

            elif self._state == STATE_CSI_INTERMEDIATE:
                self._buffer.append(byte)

                if self._is_csi_final_byte(byte):
                    # Sequence complete - filter it (don't emit)
                    debug_log(
                        self._debug_file,
                        f"PytePreprocessor: filtered CSI sequence with "
                        f"intermediate: {bytes(self._buffer)!r}"
                    )
                    self._reset_state()

                elif self._is_csi_intermediate_byte(byte):
                    # Additional intermediate byte, stay in this state
                    pass

                elif self._is_csi_parameter_byte(byte):
                    # Parameter after intermediate, stay in this state
                    pass

                else:
                    # Unexpected byte in intermediate state
                    # Don't emit the buffer (filter the malformed sequence)
                    debug_log(
                        self._debug_file,
                        f"PytePreprocessor: filtered malformed CSI sequence: "
                        f"{bytes(self._buffer)!r}"
                    )
                    self._reset_state()

            # Check buffer overflow
            if len(self._buffer) > self.MAX_BUFFER_SIZE:
                debug_log(
                    self._debug_file,
                    f"PytePreprocessor: buffer overflow ({len(self._buffer)} bytes), "
                    f"discarding: {bytes(self._buffer)!r}"
                )
                self._reset_state()

        return bytes(output)
