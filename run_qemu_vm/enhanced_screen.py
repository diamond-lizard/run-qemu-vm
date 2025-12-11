#!/usr/bin/env python3
"""
Enhanced pyte Screen with PTY response capability.

Extends pyte.Screen to enable sending responses back to the PTY,
allowing proper handling of cursor position queries and other
device status requests.
"""

import os

import pyte


class EnhancedScreen(pyte.Screen):
    """
    A pyte.Screen subclass that can send responses back to the PTY.

    pyte's Screen class has a write_process_input() method that is called
    when the terminal needs to send data back to the controlling process
    (e.g., cursor position responses to ESC[6n queries). The base
    implementation is a no-op; this subclass overrides it to actually
    write to the PTY.
    """

    def __init__(self, columns, lines):
        """
        Initialize the enhanced screen.

        Args:
            columns: Number of columns in the terminal.
            lines: Number of lines in the terminal.
        """
        super().__init__(columns, lines)
        self._pty_fd = None

    def set_pty_fd(self, fd):
        """
        Set the PTY file descriptor for sending responses.

        Args:
            fd: The master PTY file descriptor.
        """
        self._pty_fd = fd

    def write_process_input(self, data):
        """
        Write data back to the controlling process via PTY.

        This method is called by pyte when the terminal needs to send
        a response, such as the cursor position in response to ESC[6n.

        Args:
            data: String data to send to the PTY.
        """
        if self._pty_fd is not None:
            try:
                os.write(self._pty_fd, data.encode('utf-8'))
            except OSError:
                # PTY might be closed, ignore silently
                pass
