import logging
from enum import Enum, auto

# Set up a logger for this module.
logger = logging.getLogger(__name__)


class AcsState(Enum):
    """Represents the state of the ACS translator."""
    NORMAL = auto()
    ACS_MODE = auto()


class ACSTranslator:
    """
    Translates DEC Special Graphics (ACS) escape sequences and characters into
    Unicode box-drawing characters.

    This class implements a state machine to track whether the terminal is in
    ACS mode. When in ACS mode, it translates specific ASCII characters to their
    corresponding Unicode box-drawing symbols according to the DEC Special
    Graphics character set.

    The translator is stateful and designed to process a stream of bytes that
    may be split across multiple chunks. It correctly handles escape sequences
    that are broken across chunk boundaries.

    Attributes:
        _state (AcsState): The current state of the translator (NORMAL or ACS_MODE).
        _buffer (bytes): A buffer to store incomplete escape sequences from the
                         end of a data chunk.
        _acs_map (dict[int, str]): A mapping from ASCII byte values to Unicode
                                   characters for translation in ACS mode.
    """

    # DEC VT100 escape sequences for entering and exiting ACS mode.
    _ACS_ENTER_SEQ = b'\x1b(0'  # ESC ( 0
    _ACS_EXIT_SEQ = b'\x1b(B'   # ESC ( B

    # Maximum length of an incomplete sequence to buffer.
    # We only need to handle sequences up to 3 bytes long, so a buffer of
    # size 2 is sufficient to catch partials.
    _MAX_BUFFER_SIZE = 2

    def __init__(self):
        """Initializes the ACSTranslator."""
        # The character mapping is based on `dec-special-graphics-character-set.md`.
        # It maps the integer value of an ASCII byte to its Unicode replacement.
        self._acs_map = {
            0x5f: '\u00a0',  # NO-BREAK SPACE
            0x60: '\u25c6',  # BLACK DIAMOND
            0x61: '\u2592',  # MEDIUM SHADE
            0x62: '\u2409',  # SYMBOL FOR HORIZONTAL TABULATION
            0x63: '\u240c',  # SYMBOL FOR FORM FEED
            0x64: '\u240d',  # SYMBOL FOR CARRIAGE RETURN
            0x65: '\u240a',  # SYMBOL FOR LINE FEED
            0x66: '\u00b0',  # DEGREE SIGN
            0x67: '\u00b1',  # PLUS-MINUS SIGN
            0x68: '\u2424',  # SYMBOL FOR NEWLINE
            0x69: '\u240b',  # SYMBOL FOR VERTICAL TABULATION
            0x6a: '\u2518',  # BOX DRAWINGS LIGHT UP AND LEFT (┘)
            0x6b: '\u2510',  # BOX DRAWINGS LIGHT DOWN AND LEFT (┐)
            0x6c: '\u250c',  # BOX DRAWINGS LIGHT DOWN AND RIGHT (┌)
            0x6d: '\u2514',  # BOX DRAWINGS LIGHT UP AND RIGHT (└)
            0x6e: '\u253c',  # BOX DRAWINGS LIGHT VERTICAL AND HORIZONTAL (┼)
            0x6f: '\u23ba',  # HORIZONTAL SCAN LINE-1
            0x70: '\u23bb',  # HORIZONTAL SCAN LINE-3
            0x71: '\u2500',  # BOX DRAWINGS LIGHT HORIZONTAL (─)
            0x72: '\u23bc',  # HORIZONTAL SCAN LINE-7
            0x73: '\u23bd',  # HORIZONTAL SCAN LINE-9
            0x74: '\u251c',  # BOX DRAWINGS LIGHT VERTICAL AND RIGHT (├)
            0x75: '\u2524',  # BOX DRAWINGS LIGHT VERTICAL AND LEFT (┤)
            0x76: '\u2534',  # BOX DRAWINGS LIGHT UP AND HORIZONTAL (┴)
            0x77: '\u252c',  # BOX DRAWINGS LIGHT DOWN AND HORIZONTAL (┬)
            0x78: '\u2502',  # BOX DRAWINGS LIGHT VERTICAL (│)
            0x79: '\u2264',  # LESS-THAN OR EQUAL TO
            0x7a: '\u2265',  # GREATER-THAN OR EQUAL TO
            0x7b: '\u03c0',  # GREEK SMALL LETTER PI
            0x7c: '\u2260',  # NOT EQUAL TO
            0x7d: '\u00a3',  # POUND SIGN
            0x7e: '\u00b7',  # MIDDLE DOT
        }
        self.reset()

    def reset(self):
        """
        Resets the translator to its initial state.
        This clears the sequence buffer and sets the mode to NORMAL. This should
        be called when the underlying PTY connection is reset.
        """
        logger.debug("ACSTranslator reset.")
        self._state = AcsState.NORMAL
        self._buffer = b''

    def translate(self, data: bytes) -> bytes:
        """
        Processes a chunk of byte data, translating ACS characters to Unicode.

        This method scans for ACS escape sequences, updates the internal state,
        and translates characters when in ACS_MODE. It handles partial escape
        sequences at chunk boundaries by using an internal buffer.

        Args:
            data: A chunk of raw bytes from the PTY.

        Returns:
            A new byte string with ACS characters translated to their UTF-8
            encoded Unicode equivalents.
        """
        # Prepend any buffered data from the previous call.
        if self._buffer:
            data = self._buffer + data
            self._buffer = b''

        if not data:
            return b''

        output = bytearray()
        i = 0
        n = len(data)

        try:
            while i < n:
                # Check for escape sequences.
                if data[i] == 0x1b:  # ESC
                    # Check if the sequence is split at the end of the chunk.
                    if i + 1 >= n:  # Incomplete, just ESC
                        self._buffer = data[i:]
                        break
                    if i + 2 >= n:  # Incomplete, ESC and one more char
                        # Only buffer if it could be a partial ACS sequence.
                        if data[i:i+2] == self._ACS_ENTER_SEQ[:2]:
                            self._buffer = data[i:]
                            break
                        # Otherwise, it's not an ACS sequence we care about.
                        # Let it pass through.

                # Check for full ACS sequences.
                if data[i:i+3] == self._ACS_ENTER_SEQ:
                    if self._state != AcsState.ACS_MODE:
                        logger.debug("Entering ACS mode.")
                        self._state = AcsState.ACS_MODE
                    i += 3
                    continue
                elif data[i:i+3] == self._ACS_EXIT_SEQ:
                    if self._state != AcsState.NORMAL:
                        logger.debug("Exiting ACS mode.")
                        self._state = AcsState.NORMAL
                    i += 3
                    continue

                # If in ACS mode, translate the character.
                if self._state == AcsState.ACS_MODE:
                    char_byte = data[i]
                    if char_byte in self._acs_map:
                        # Character has an ACS mapping.
                        unicode_char = self._acs_map[char_byte]
                        output.extend(unicode_char.encode('utf-8'))
                        if logger.getEffectiveLevel() <= logging.DEBUG:
                            logger.debug("Translated 0x%x to '%s'", char_byte, unicode_char)
                    else:
                        # No mapping, pass through.
                        output.append(char_byte)
                else:
                    # Not in ACS mode, pass through.
                    output.append(data[i])

                i += 1
        except Exception:
            logger.exception("Error during ACS translation. Returning original data.")
            # On error, it's safer to return the original data to avoid
            # losing information, even if it's garbled.
            return data

        return bytes(output)
