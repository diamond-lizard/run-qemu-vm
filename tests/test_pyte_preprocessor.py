#!/usr/bin/env python3
"""
Unit tests for PytePreprocessor.

Tests the stateful CSI sequence filter including:
- Normal text passthrough
- Complete CSI+intermediate sequences in single chunk
- CSI+intermediate split across chunks
- Buffer overflow handling
- Extreme cursor position filtering
"""

import io
from unittest.mock import Mock

import pytest

from run_qemu_vm.pyte_preprocessor import (
    PytePreprocessor,
    STATE_NORMAL,
    STATE_ESC_SEEN,
    STATE_CSI_STARTED,
    STATE_CSI_INTERMEDIATE,
)


@pytest.fixture
def mock_screen():
    """Create a mock screen with standard dimensions."""
    screen = Mock()
    screen.lines = 24
    screen.columns = 80
    return screen


@pytest.fixture
def preprocessor(mock_screen):
    """Create a preprocessor with mock screen."""
    return PytePreprocessor(mock_screen)


@pytest.fixture
def preprocessor_with_debug(mock_screen):
    """Create a preprocessor with debug logging enabled."""
    debug_file = io.StringIO()
    return PytePreprocessor(mock_screen, debug_file), debug_file


class TestNormalTextPassthrough:
    """Tests for normal text passing through unchanged."""

    def test_plain_ascii(self, preprocessor):
        """Plain ASCII text should pass through unchanged."""
        data = b"Hello, World!"
        assert preprocessor.filter(data) == data

    def test_utf8_text(self, preprocessor):
        """UTF-8 encoded text should pass through unchanged."""
        data = "Hello, \xe4\xb8\x96\xe7\x95\x8c!".encode('utf-8')
        assert preprocessor.filter(data) == data

    def test_empty_input(self, preprocessor):
        """Empty input should return empty output."""
        assert preprocessor.filter(b"") == b""

    def test_newlines_and_control_chars(self, preprocessor):
        """Newlines and control characters should pass through."""
        data = b"line1\r\nline2\tindented"
        assert preprocessor.filter(data) == data


class TestNormalCSIPassthrough:
    """Tests for normal CSI sequences passing through."""

    def test_cursor_move_sequence(self, preprocessor):
        """Normal cursor movement sequences should pass through."""
        data = b"\x1b[10;20H"  # Move to row 10, col 20
        assert preprocessor.filter(data) == data

    def test_sgr_sequence(self, preprocessor):
        """SGR (color/style) sequences should pass through."""
        data = b"\x1b[1;31m"  # Bold red
        assert preprocessor.filter(data) == data

    def test_cursor_up_sequence(self, preprocessor):
        """Cursor up sequence should pass through."""
        data = b"\x1b[5A"  # Move up 5 lines
        assert preprocessor.filter(data) == data

    def test_erase_display(self, preprocessor):
        """Erase display sequence should pass through."""
        data = b"\x1b[2J"  # Clear screen
        assert preprocessor.filter(data) == data

    def test_mixed_text_and_sequences(self, preprocessor):
        """Text mixed with normal sequences should pass through."""
        data = b"Hello \x1b[1mWorld\x1b[0m!"
        assert preprocessor.filter(data) == data


class TestCSIIntermediateFiltering:
    """Tests for CSI sequences with intermediate characters."""

    def test_decstr_single_chunk(self, preprocessor):
        """DECSTR (ESC[!p) in single chunk should be filtered."""
        data = b"\x1b[!p"
        assert preprocessor.filter(data) == b""

    def test_decstr_with_surrounding_text(self, preprocessor):
        """DECSTR with surrounding text should filter only DECSTR."""
        data = b"before\x1b[!pafter"
        assert preprocessor.filter(data) == b"beforeafter"

    def test_decscusr_sequence(self, preprocessor):
        """DECSCUSR (ESC[ SP q) should be filtered."""
        data = b"\x1b[ q"  # Space is intermediate char
        assert preprocessor.filter(data) == b""

    def test_multiple_intermediate_sequences(self, preprocessor):
        """Multiple intermediate sequences should all be filtered."""
        data = b"\x1b[!ptext\x1b[ q"
        assert preprocessor.filter(data) == b"text"


class TestChunkedInput:
    """Tests for sequences split across multiple chunks."""

    def test_decstr_split_at_esc(self, preprocessor):
        """DECSTR split after ESC should be filtered."""
        result1 = preprocessor.filter(b"text\x1b")
        result2 = preprocessor.filter(b"[!pmore")
        assert result1 + result2 == b"textmore"

    def test_decstr_split_at_bracket(self, preprocessor):
        """DECSTR split after ESC[ should be filtered."""
        result1 = preprocessor.filter(b"text\x1b[")
        result2 = preprocessor.filter(b"!pmore")
        assert result1 + result2 == b"textmore"

    def test_decstr_split_at_intermediate(self, preprocessor):
        """DECSTR split after intermediate char should be filtered."""
        result1 = preprocessor.filter(b"text\x1b[!")
        result2 = preprocessor.filter(b"pmore")
        assert result1 + result2 == b"textmore"

    def test_normal_csi_split_at_esc(self, preprocessor):
        """Normal CSI split after ESC should pass through."""
        result1 = preprocessor.filter(b"text\x1b")
        result2 = preprocessor.filter(b"[1mmore")
        assert result1 + result2 == b"text\x1b[1mmore"

    def test_normal_csi_split_at_bracket(self, preprocessor):
        """Normal CSI split after ESC[ should pass through."""
        result1 = preprocessor.filter(b"text\x1b[")
        result2 = preprocessor.filter(b"1mmore")
        assert result1 + result2 == b"text\x1b[1mmore"

    def test_state_preserved_across_calls(self, preprocessor):
        """State should be preserved between filter() calls."""
        preprocessor.filter(b"\x1b[")
        assert preprocessor._state == STATE_CSI_STARTED
        preprocessor.filter(b"!")
        assert preprocessor._state == STATE_CSI_INTERMEDIATE


class TestExtremeCursorFiltering:
    """Tests for extreme cursor position filtering."""

    def test_extreme_row_filtered(self, mock_screen):
        """Cursor position with extreme row should be filtered."""
        mock_screen.lines = 24
        mock_screen.columns = 80
        pp = PytePreprocessor(mock_screen)
        data = b"\x1b[1000;10H"  # Row 1000, col 10
        assert pp.filter(data) == b""

    def test_extreme_col_filtered(self, mock_screen):
        """Cursor position with extreme column should be filtered."""
        mock_screen.lines = 24
        mock_screen.columns = 80
        pp = PytePreprocessor(mock_screen)
        data = b"\x1b[10;1000H"  # Row 10, col 1000
        assert pp.filter(data) == b""

    def test_extreme_both_filtered(self, mock_screen):
        """Cursor position with both extreme coords should be filtered."""
        mock_screen.lines = 24
        mock_screen.columns = 80
        pp = PytePreprocessor(mock_screen)
        data = b"\x1b[32766;32766H"
        assert pp.filter(data) == b""

    def test_normal_cursor_passes(self, mock_screen):
        """Normal cursor position within bounds should pass through."""
        mock_screen.lines = 24
        mock_screen.columns = 80
        pp = PytePreprocessor(mock_screen)
        data = b"\x1b[10;40H"  # Row 10, col 40 - within 24x80
        assert pp.filter(data) == data

    def test_boundary_cursor_passes(self, mock_screen):
        """Cursor at exact boundary should pass through."""
        mock_screen.lines = 24
        mock_screen.columns = 80
        pp = PytePreprocessor(mock_screen)
        data = b"\x1b[24;80H"  # Exactly at boundary
        assert pp.filter(data) == data

    def test_one_over_boundary_filtered(self, mock_screen):
        """Cursor one past boundary should be filtered."""
        mock_screen.lines = 24
        mock_screen.columns = 80
        pp = PytePreprocessor(mock_screen)
        data = b"\x1b[25;80H"  # Row 25, one past boundary
        assert pp.filter(data) == b""


class TestBufferOverflow:
    """Tests for buffer overflow handling."""

    def test_buffer_overflow_discards_and_logs(self, mock_screen):
        """Buffer overflow should discard sequence and log warning."""
        debug_file = io.StringIO()
        pp = PytePreprocessor(mock_screen, debug_file)

        # Start a sequence and feed lots of parameter bytes
        pp.filter(b"\x1b[")
        pp.filter(b"1" * 100)  # Way more than 64 bytes

        # Check state was reset
        assert pp._state == STATE_NORMAL
        assert len(pp._buffer) == 0

        # Check warning was logged
        log_output = debug_file.getvalue()
        assert "buffer overflow" in log_output

    def test_no_overflow_warning_without_debug_file(self, mock_screen):
        """Buffer overflow without debug file should not crash."""
        pp = PytePreprocessor(mock_screen, debug_file=None)
        pp.filter(b"\x1b[")
        pp.filter(b"1" * 100)
        assert pp._state == STATE_NORMAL


class TestNonCSIEscapeSequences:
    """Tests for non-CSI escape sequences."""

    def test_esc_followed_by_letter(self, preprocessor):
        """ESC followed by non-[ should pass through."""
        data = b"\x1bM"  # Reverse index
        assert preprocessor.filter(data) == data

    def test_esc_followed_by_paren(self, preprocessor):
        """ESC( sequence should pass through."""
        data = b"\x1b(0"  # Enter ACS mode
        assert preprocessor.filter(data) == data

    def test_esc_at_end_of_chunk(self, preprocessor):
        """Lone ESC at end of chunk should be buffered."""
        result1 = preprocessor.filter(b"text\x1b")
        assert result1 == b"text"
        assert preprocessor._state == STATE_ESC_SEEN

        # Non-[ should emit ESC and the byte
        result2 = preprocessor.filter(b"M")
        assert result2 == b"\x1bM"
        assert preprocessor._state == STATE_NORMAL


class TestDebugLogging:
    """Tests for debug logging functionality."""

    def test_filtered_sequence_logged(self, preprocessor_with_debug):
        """Filtered CSI+intermediate should be logged."""
        pp, debug_file = preprocessor_with_debug
        pp.filter(b"\x1b[!p")
        log_output = debug_file.getvalue()
        assert "filtered CSI sequence" in log_output
        assert "intermediate" in log_output

    def test_filtered_cursor_logged(self, mock_screen):
        """Filtered extreme cursor should be logged."""
        debug_file = io.StringIO()
        mock_screen.lines = 24
        mock_screen.columns = 80
        pp = PytePreprocessor(mock_screen, debug_file)
        pp.filter(b"\x1b[1000;1000H")
        log_output = debug_file.getvalue()
        assert "extreme cursor position" in log_output

    def test_no_logging_when_disabled(self, preprocessor):
        """No crash when filtering with debug_file=None."""
        preprocessor.filter(b"\x1b[!p")
        # Should not raise any exception
