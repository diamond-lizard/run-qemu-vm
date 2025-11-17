import pytest

from run_qemu_vm.acs_translator import ACSTranslator, AcsState


@pytest.fixture
def translator():
    """Provides a fresh ACSTranslator instance for each test."""
    return ACSTranslator()


# --- Basic Functionality Tests ---


def test_normal_text_passes_through(translator: ACSTranslator):
    """Test that normal ASCII text is not modified."""
    data = b"Hello, World!"
    assert translator.translate(data) == data
    assert translator._state == AcsState.NORMAL


def test_enter_acs_mode(translator: ACSTranslator):
    """Test that the enter ACS sequence is consumed and state changes."""
    data = b"\x1b(0"
    assert translator.translate(data) == b""
    assert translator._state == AcsState.ACS_MODE


def test_exit_acs_mode(translator: ACSTranslator):
    """Test that exiting ACS mode works and the sequence is consumed."""
    translator.translate(b"\x1b(0")  # Enter ACS mode
    assert translator._state == AcsState.ACS_MODE
    assert translator.translate(b"\x1b(B") == b""
    assert translator._state == AcsState.NORMAL


def test_acs_chars_translated_in_acs_mode(translator: ACSTranslator):
    """Test that ACS characters are translated correctly when in ACS mode."""
    # lqk -> ┌─┐
    data = b"\x1b(0lqk\x1b(B"
    expected = "┌─┐".encode("utf-8")
    assert translator.translate(data) == expected


def test_acs_chars_not_translated_in_normal_mode(translator: ACSTranslator):
    """Test that ACS characters are passed through when not in ACS mode."""
    data = b"lqk"
    assert translator.translate(data) == data


def test_mixed_content_translation(translator: ACSTranslator):
    """Test translation of mixed regular and ACS characters."""
    data = b"box: \x1b(0lqj\x1b(B."
    expected = b"box: " + "┌─┘".encode("utf-8") + b"."
    assert translator.translate(data) == expected


# --- Buffering and Chunking Tests ---


def test_sequence_split_after_esc(translator: ACSTranslator):
    """Test handling of a sequence split after the ESC byte."""
    assert translator.translate(b"text\x1b") == b"text"
    assert translator._buffer == b"\x1b"
    assert translator._state == AcsState.NORMAL

    assert translator.translate(b"(0more") == b"more"
    assert translator._buffer == b""
    assert translator._state == AcsState.ACS_MODE


def test_sequence_split_after_esc_paren(translator: ACSTranslator):
    """Test handling of a sequence split after ESC (."""
    assert translator.translate(b"text\x1b(") == b"text"
    assert translator._buffer == b"\x1b("
    assert translator._state == AcsState.NORMAL

    assert translator.translate(b"0more") == b"more"
    assert translator._buffer == b""
    assert translator._state == AcsState.ACS_MODE


def test_buffer_cleared_on_next_non_matching_chunk(translator: ACSTranslator):
    """Test that the buffer is flushed if the next chunk doesn't complete the sequence."""
    assert translator.translate(b"text\x1b") == b"text"
    assert translator._buffer == b"\x1b"

    # The next chunk does not complete the ACS sequence.
    # The buffered '\x1b' should be passed through.
    assert translator.translate(b"not-an-acs-seq") == b"\x1bnot-an-acs-seq"
    assert translator._buffer == b""
    assert translator._state == AcsState.NORMAL


# --- Edge Case Tests ---


def test_empty_input(translator: ACSTranslator):
    """Test that empty input produces empty output."""
    assert translator.translate(b"") == b""


def test_only_escape_sequences(translator: ACSTranslator):
    """Test input containing only ACS escape sequences."""
    assert translator.translate(b"\x1b(0\x1b(B") == b""


def test_rapid_mode_switching(translator: ACSTranslator):
    """Test rapid switching between NORMAL and ACS modes."""
    data = b"\x1b(0l\x1b(B-\x1b(0k\x1b(B"
    expected = "┌".encode("utf-8") + b"-" + "┐".encode("utf-8")
    assert translator.translate(data) == expected


def test_malformed_acs_sequence_passes_through(translator: ACSTranslator):
    """Test that a malformed ACS sequence (e.g., ESC ( X) is passed through."""
    data = b"hello \x1b(X world"
    assert translator.translate(data) == data


def test_other_escape_sequences_pass_through(translator: ACSTranslator):
    """Test that non-ACS ANSI escape sequences (e.g., for color) are ignored."""
    data = b"\x1b[31mRed text\x1b[0m"
    assert translator.translate(data) == data


# --- Reset Functionality Test ---


def test_reset_clears_state_and_buffer(translator: ACSTranslator):
    """Test that the reset() method correctly resets the translator's state."""
    # 1. Put translator into ACS mode with a partial sequence in the buffer.
    translator.translate(b"\x1b(0some text\x1b")
    assert translator._state == AcsState.ACS_MODE
    assert translator._buffer == b"\x1b"

    # 2. Reset the translator.
    translator.reset()
    assert translator._state == AcsState.NORMAL
    assert translator._buffer == b""

    # 3. Verify that it's back in normal mode and doesn't translate.
    assert translator.translate(b"lqk") == b"lqk"


# --- Integration-style Tests ---


def test_integration_simple_box(translator: ACSTranslator):
    """Test a multi-line sequence that draws a simple box."""
    # This sequence draws:
    # ┌───┐
    # │ │
    # └───┘
    data = b"\x1b(0lqqqk\x1b(B\n\x1b(0x\x1b(B \x1b(0x\x1b(B\n\x1b(0mqqqj\x1b(B"
    expected = (
        "┌───┐".encode("utf-8")
        + b"\n"
        + "│ │".encode("utf-8")
        + b"\n"
        + "└───┘".encode("utf-8")
    )
    assert translator.translate(data) == expected


def test_integration_box_with_text(translator: ACSTranslator):
    """Test a sequence with text interspersed with ACS characters."""
    # This sequence draws: ┌─┐ Hello └┘│
    data = b"\x1b(0lqk\x1b(B Hello \x1b(0mjx\x1b(B"
    expected = "┌─┐".encode("utf-8") + b" Hello " + "└┘│".encode("utf-8")
    assert translator.translate(data) == expected


def test_integration_with_ansi_colors(translator: ACSTranslator):
    """Test that ACS translation works correctly with ANSI color codes."""
    # This sequence draws a red ┌─┐
    data = b"\x1b[31m\x1b(0lqk\x1b(B\x1b[0m"
    expected = b"\x1b[31m" + "┌─┐".encode("utf-8") + b"\x1b[0m"
    assert translator.translate(data) == expected
