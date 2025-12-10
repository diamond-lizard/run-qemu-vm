#!/usr/bin/env python3
"""
Shared logging utilities for run-qemu-vm.

Provides timestamped debug logging to file for diagnostic purposes.
"""

import time


def debug_log(debug_file, message):
    """
    Write a timestamped debug message to the debug file if enabled.

    Args:
        debug_file: An open file handle for writing debug messages,
                    or None if debug logging is disabled.
        message: The debug message string to write.

    Returns:
        None
    """
    if debug_file:
        try:
            timestamp = time.time()
            debug_file.write(f"[{timestamp:.6f}] {message}\n")
            debug_file.flush()
        except (ValueError, OSError):
            # File might be closed if called from atexit, ignore silently
            pass
