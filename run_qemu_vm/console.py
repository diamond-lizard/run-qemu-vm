import asyncio
import fcntl
import os
import select
import shutil
import socket
import struct
import subprocess
import sys
import termios
import time
import traceback
from asyncio import Queue
from pathlib import Path

import pyte
from prompt_toolkit.application import Application
from prompt_toolkit.data_structures import Point
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.document import Document
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import ANSI, to_formatted_text
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout.containers import ConditionalContainer, HSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.layout import Layout
from prompt_toolkit.layout.processors import (
    Processor,
    Transformation,
    TransformationInput,
)
from prompt_toolkit.widgets import Button, Dialog

from . import config as app_config


def _debug_log(debug_file, message):
    """Writes a timestamped debug message to the debug file if enabled."""
    if debug_file:
        timestamp = time.time()
        debug_file.write(f"[{timestamp:.6f}] {message}\n")
        debug_file.flush()


class AnsiColorProcessor(Processor):
    """
    A stateless processor that handles ANSI escape sequences.
    It processes the entire buffer on each rendering pass to avoid state
    corruption from partial ANSI sequences.
    """

    def apply_transformation(
        self, transformation_input: TransformationInput
    ) -> Transformation:
        """
        Parses the entire buffer text, converting ANSI codes to formatted text
        and ensuring newlines are correctly preserved as line breaks.
        """
        full_text = transformation_input.document.text
        fragments = []

        # Don't use `splitlines` as it swallows the final newline if present
        lines = full_text.split("\n")

        for i, line in enumerate(lines):
            if line:
                line_fragments = to_formatted_text(ANSI(line))
                fragments.extend(line_fragments)

            if i < len(lines) - 1:
                fragments.append(("", "\n"))

        return Transformation(fragments)


def _get_char_style(char):
    """Generates a prompt_toolkit style string from a pyte character."""
    style_parts = []

    is_default_fg = char.fg == "default"
    is_default_bg = char.bg == "default"

    # If reverse video is on with default colors, we must explicitly define
    # a swap, otherwise prompt-toolkit may render it as invisible if the
    # terminal's default fg/bg are the same.
    if char.reverse and is_default_fg and is_default_bg:
        fg_color = "black"
        bg_color = "#bbbbbb"  # A light grey for good contrast
    else:
        fg_color = char.fg
        bg_color = char.bg

    # Foreground color
    if fg_color.startswith("#"):
        style_parts.append(fg_color)
    elif fg_color != "default":
        style_parts.append(f"fg:ansi{fg_color}")

    # Background color
    if bg_color.startswith("#"):
        style_parts.append(f"bg:{bg_color}")
    elif bg_color != "default":
        style_parts.append(f"bg:ansi{bg_color}")

    # Attributes
    if char.bold:
        style_parts.append("bold")
    if char.italics:
        style_parts.append("italic")
    if char.underscore:
        style_parts.append("underline")
    if char.reverse:
        style_parts.append("reverse")

    return " ".join(style_parts)


def _process_line_to_fragments(y, pyte_screen):
    """
    Processes a single line from the pyte screen into styled fragments.

    This function combines the character data from `pyte_screen.display` (which
    correctly translates ACS characters) with the style information from
    `pyte_screen.buffer` to produce a list of `(style, text)` fragments.
    This version creates one fragment per character for simplicity and robustness.
    """
    line_fragments = []
    # pyte_screen.display contains the rendered text, including ACS translations.
    display_line = pyte_screen.display[y]

    for x in range(pyte_screen.columns):
        # Get the character's style information from the buffer.
        char_style_info = pyte_screen.buffer[y][x]

        # Get the character's data from the display property.
        # Pad with spaces if the display line is shorter than the screen width,
        # as .display can have ragged right edges.
        char_data = display_line[x] if x < len(display_line) else " "

        style_str = _get_char_style(char_style_info)
        line_fragments.append((style_str, char_data))

    return line_fragments


def _get_pty_screen_fragments(pyte_screen, attr_log_file=None, debug_file=None):
    """Converts the entire pyte screen state into prompt_toolkit fragments."""
    _debug_log(debug_file, f"RENDER: Starting fragment generation. Cursor at ({pyte_screen.cursor.x}, {pyte_screen.cursor.y})")
    
    render_start = time.time()
    fragments = []
    if attr_log_file:
        attr_log_file.write("--- NEW FRAME ---\n")
        attr_log_file.flush()

    non_empty_lines = 0
    total_fragments = 0
    
    # Process ALL lines without optimization - we need to see what's actually there
    for y in range(pyte_screen.lines):
        line_fragments = _process_line_to_fragments(y, pyte_screen)

        if attr_log_file:
            # Log any fragment that is not a default-styled space character.
            log_parts = []
            for style, text in line_fragments:
                if style != "" or text != " ":
                    log_parts.append(f"[{style}]{repr(text)}")

            if log_parts:
                attr_log_file.write(f"Line {y:2d}: {''.join(log_parts)}\n")
                attr_log_file.flush()
                non_empty_lines += 1

        fragments.extend(line_fragments)
        fragments.append(("", "\n"))
        total_fragments += len(line_fragments) + 1

    render_duration = time.time() - render_start
    _debug_log(debug_file, f"RENDER: Generated {total_fragments} fragments from {non_empty_lines} non-empty lines in {render_duration:.6f}s")
    
    # Debug: Log the actual pyte screen state
    if debug_file:
        _debug_log(debug_file, f"PYTE_STATE: Screen size {pyte_screen.lines}x{pyte_screen.columns}")
        _debug_log(debug_file, f"PYTE_STATE: Cursor at ({pyte_screen.cursor.x}, {pyte_screen.cursor.y})")
        _debug_log(debug_file, f"PYTE_STATE: Display has {len(pyte_screen.display)} lines")
        for i, line in enumerate(pyte_screen.display[:10]):  # First 10 lines
            if line.strip():
                _debug_log(debug_file, f"PYTE_STATE: Display[{i}] = {repr(line)}")
    
    return fragments


def _append_to_log(log_buffer: Buffer, text_to_append: str):
    """Appends text to the log buffer, bypassing the read-only protection."""
    current_doc = log_buffer.document
    new_text = current_doc.text + text_to_append
    new_doc = Document(text=new_text, cursor_position=len(new_text))
    log_buffer.set_document(new_doc, bypass_readonly=True)


def _open_pty_device(pty_device, debug_file=None):
    """Opens the PTY device file descriptor."""
    _debug_log(debug_file, f"PTY: Opening device {pty_device}")
    try:
        # Use a standard blocking file descriptor. The read operations will be
        # handled in a separate thread by the asyncio executor.
        pty_fd = os.open(pty_device, os.O_RDWR | os.O_NOCTTY)
        _debug_log(debug_file, f"PTY: Successfully opened, fd={pty_fd}")
    except FileNotFoundError:
        print(f"Error: PTY device '{pty_device}' not found.", file=sys.stderr)
        raise

    return pty_fd


def _set_pty_size(pty_fd, rows, cols, debug_file=None):
    """
    Sets the window size of the PTY using ioctl TIOCSWINSZ.
    This informs the guest OS of the correct terminal dimensions.
    """
    if pty_fd < 0:
        return
    try:
        # struct winsize { unsigned short ws_row; unsigned short ws_col; ... }
        winsize = struct.pack("HHHH", rows, cols, 0, 0)
        fcntl.ioctl(pty_fd, termios.TIOCSWINSZ, winsize)
        _debug_log(debug_file, f"PTY: Set window size to {rows}x{cols}")
    except OSError:
        # Ignore errors if the PTY is closed or invalid
        pass


async def _connect_to_monitor(monitor_socket_path, debug_file=None):
    """Establishes a non-blocking connection to the QEMU monitor socket."""
    _debug_log(debug_file, f"MONITOR: Connecting to {monitor_socket_path}")
    monitor_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    monitor_sock.setblocking(False)
    try:
        await asyncio.get_running_loop().sock_connect(monitor_sock, monitor_socket_path)
        _debug_log(debug_file, "MONITOR: Connected successfully")
        return monitor_sock
    except (FileNotFoundError, ConnectionRefusedError):
        print("Warning: Could not connect to QEMU monitor.", file=sys.stderr)
        _debug_log(debug_file, "MONITOR: Connection failed")
        return None


async def _show_control_menu():
    """
    Displays a custom control menu with single-key shortcuts.

    This dialog allows the user to select an action using either mouse clicks,
    arrow keys + Enter, or single-character shortcuts (R, E, Q).
    """
    # This variable will hold the Application instance. The button handlers
    # close over it to be able to exit the dialog.
    app = None

    buttons_with_shortcuts = [
        ("(R)esume Console", "resume", "r"),
        ("(E)nter Monitor", "monitor", "e"),
        ("(Q)uit QEMU", "quit", "q"),
    ]

    def create_handler(value):
        """Creates a button handler that exits the app with a given result."""

        def handler():
            if app:
                app.exit(result=value)

        return handler

    # Calculate the width needed for buttons.
    # Find the longest button text and add padding for button decorations.
    # Buttons typically need extra space for borders, padding, and focus indicators.
    max_button_text_length = max(len(text) for text, _, _ in buttons_with_shortcuts)
    # Add 4 characters for button padding/borders (2 on each side is typical)
    button_width = max_button_text_length + 4

    dialog_buttons = [
        Button(text=text, handler=create_handler(value), width=button_width)
        for text, value, _ in buttons_with_shortcuts
    ]

    key_bindings = KeyBindings()
    for _, value, key in buttons_with_shortcuts:
        # Use a lambda with a default argument to capture the value of `value`
        # for each key binding.
        key_bindings.add(key.lower(), eager=True)(
            lambda event, v=value: event.app.exit(result=v)
        )
        key_bindings.add(key.upper(), eager=True)(
            lambda event, v=value: event.app.exit(result=v)
        )

    # Create a body text that is at least as wide as the buttons to ensure
    # the dialog allocates sufficient width. We pad the text to match the
    # button width to give the layout engine a hint about minimum width.
    body_text = "Select an action:".ljust(button_width)

    dialog = Dialog(
        title="Control Menu",
        body=Window(content=FormattedTextControl(body_text)),
        buttons=dialog_buttons,
        modal=True,
    )

    layout = Layout(dialog)

    app = Application(
        layout=layout,
        key_bindings=key_bindings,
        full_screen=True,
        mouse_support=True,
    )

    return await app.run_async()


async def _handle_quit_action(app, monitor_sock, debug_file=None):
    """Handles the 'quit' action from the control menu."""
    _debug_log(debug_file, "MENU: User selected quit")
    if monitor_sock:
        try:
            monitor_sock.send(b"quit\n")
            await asyncio.sleep(0.1)
        except OSError:
            pass  # Socket might be closed
    app.exit(result="User quit")


async def _handle_monitor_action(app, current_mode, monitor_sock, debug_file=None):
    """Handles the 'monitor' action from the control menu."""
    _debug_log(debug_file, "MENU: User selected monitor mode")
    # Switch mode first
    current_mode[0] = app_config.MODE_QEMU_MONITOR

    # Force a full redraw
    app.invalidate()
    app.renderer.reset()

    # Send a newline to trigger the monitor prompt to be displayed
    if monitor_sock:
        try:
            loop = asyncio.get_running_loop()
            await loop.sock_sendall(monitor_sock, b"\n")
            # Give the monitor a moment to respond
            await asyncio.sleep(0.3)
            # Force another redraw after data arrives
            app.invalidate()
        except OSError:
            pass


async def _handle_resume_action(app, current_mode, debug_file=None):
    """Handles the 'resume' action from the control menu."""
    _debug_log(debug_file, "MENU: User selected resume console")
    current_mode[0] = app_config.MODE_SERIAL_CONSOLE
    app.renderer.reset()
    app.invalidate()


async def _process_control_menu_choice(
    result, app, current_mode, monitor_sock, debug_file=None
):
    """Handles the action selected from the control menu."""
    if result == "quit":
        await _handle_quit_action(app, monitor_sock, debug_file)
    elif result == "monitor":
        await _handle_monitor_action(app, current_mode, monitor_sock, debug_file)
    else:  # resume
        await _handle_resume_action(app, current_mode, debug_file)


async def _handle_control_menu(
    app, current_mode, monitor_sock, menu_is_active, debug_file=None
):
    """Displays the control menu and handles the user's choice."""
    _debug_log(debug_file, "MENU: Opening control menu")
    menu_is_active[0] = True
    try:
        result = await _show_control_menu()
        await _process_control_menu_choice(
            result, app, current_mode, monitor_sock, debug_file
        )
    finally:
        menu_is_active[0] = False
        app.invalidate()


async def _forward_input(event, current_mode, pty_fd, monitor_sock, debug_file=None):
    """Forwards user input to the PTY or monitor based on the current mode."""
    data_repr = repr(event.data)
    _debug_log(debug_file, f"INPUT: Forwarding {data_repr} in mode {current_mode[0]}")
    
    loop = asyncio.get_running_loop()
    try:
        if current_mode[0] == app_config.MODE_SERIAL_CONSOLE:
            await loop.run_in_executor(
                None, os.write, pty_fd, event.data.encode("utf-8", errors="replace")
            )
            _debug_log(debug_file, f"INPUT: Wrote {data_repr} to PTY")
        elif current_mode[0] == app_config.MODE_QEMU_MONITOR and monitor_sock:
            await loop.sock_sendall(
                monitor_sock, event.data.encode("utf-8", errors="replace")
            )
            _debug_log(debug_file, f"INPUT: Wrote {data_repr} to monitor")
    except OSError as e:
        _debug_log(debug_file, f"INPUT: Write error: {e}")


def _create_key_bindings(current_mode, pty_fd, monitor_sock, menu_is_active, debug_file=None):
    """Creates and configures the key bindings for the console application."""
    key_bindings = KeyBindings()

    # Eagerly bind Ctrl-] to open the control menu.
    # This must be eager to preempt prompt_toolkit's default behavior for some
    # control characters and the <any> fallback.
    @key_bindings.add("\x1d", eager=True)  # Ctrl-]
    async def _(event):
        await _handle_control_menu(
            event.app, current_mode, monitor_sock, menu_is_active, debug_file
        )

    # Eagerly bind Ctrl-C to forward it to the guest.
    # This is critical to prevent prompt_toolkit from raising KeyboardInterrupt
    # and terminating the application.
    @key_bindings.add("c-c", eager=True)
    async def _(event):
        await _forward_input(event, current_mode, pty_fd, monitor_sock, debug_file)

    # The <any> binding is a non-eager fallback. It will catch any key
    # sequence that is not handled by a more specific (or eager) binding.
    # This includes all printable characters (like 'e' for GRUB) and other
    # unhandled control sequences.
    @key_bindings.add("<any>")
    async def _(event):
        await _forward_input(event, current_mode, pty_fd, monitor_sock, debug_file)

    return key_bindings


def _create_console_layout(
    get_pty_screen_fragments,
    get_monitor_screen_fragments,
    current_mode,
    pyte_screen,
    monitor_pyte_screen,
):
    """Creates the prompt_toolkit layout for the console."""
    is_serial_mode = Condition(lambda: current_mode[0] == app_config.MODE_SERIAL_CONSOLE)

    def get_pty_cursor_position():
        """Gets the cursor position from the PTY pyte screen."""
        # This function is called by prompt-toolkit without arguments.
        # Clamp cursor position to valid screen bounds to prevent display issues
        x = max(0, min(pyte_screen.cursor.x, pyte_screen.columns - 1))
        y = max(0, min(pyte_screen.cursor.y, pyte_screen.lines - 1))
        return Point(x=x, y=y)

    pty_control = FormattedTextControl(
        text=get_pty_screen_fragments, get_cursor_position=get_pty_cursor_position
    )
    pty_container = ConditionalContainer(
        Window(content=pty_control, dont_extend_height=True), filter=is_serial_mode
    )

    def get_monitor_cursor_position():
        """Gets the cursor position from the monitor pyte screen."""
        # This function is called by prompt-toolkit without arguments.
        # Clamp cursor position to valid screen bounds to prevent display issues
        x = max(0, min(monitor_pyte_screen.cursor.x, monitor_pyte_screen.columns - 1))
        y = max(0, min(monitor_pyte_screen.cursor.y, monitor_pyte_screen.lines - 1))
        return Point(x=x, y=y)

    monitor_control = FormattedTextControl(
        text=get_monitor_screen_fragments,
        get_cursor_position=get_monitor_cursor_position,
    )
    monitor_container = ConditionalContainer(
        Window(content=monitor_control, dont_extend_height=True),
        filter=~is_serial_mode,
    )

    return Layout(HSplit([pty_container, monitor_container]))


async def _log_task_error(log_queue: Queue, task_name: str, error_type: str = "ERROR"):
    """Logs a traceback for a crashed asyncio task."""
    tb_str = traceback.format_exc()
    await log_queue.put(f"\n--- {task_name} {error_type} ---\n{tb_str}")


def _filter_cursor_position_queries(data):
    """
    Filters out cursor position query sequences (CPR) from the data stream.
    
    These sequences (ESC[6n) are sometimes sent by the guest and can cause
    cursor position issues. We filter them out to prevent interference.
    
    Returns: (filtered_data, had_cpr)
    """
    # Pattern for cursor position query: ESC[6n
    # Pattern for cursor position report: ESC[row;colR
    # Pattern for extreme cursor movements: ESC[32766;32766H (or similar large numbers)
    
    original_len = len(data)
    
    # Remove cursor position queries (ESC[6n)
    filtered = data.replace(b'\x1b[6n', b'')
    
    # Remove extreme cursor position movements (anything moving to row/col > 1000)
    # This is a heuristic to catch ESC[32766;32766H type sequences
    import re
    # Match ESC[<large_num>;<large_num>H where large_num > 1000
    filtered = re.sub(b'\x1b\\[(\\d{4,});(\\d{4,})H', b'', filtered)
    
    had_cpr = len(filtered) != original_len
    
    return filtered, had_cpr


def _process_pty_data(data, app, pyte_stream, menu_is_active, debug_file=None):
    """
    Feeds PTY data to the pyte stream and triggers UI invalidation.
    """
    start_time = time.time()
    _debug_log(debug_file, f"PTY_DATA: Processing {len(data)} bytes")
    
    # Filter out cursor position queries before processing
    filtered_data, had_cpr = _filter_cursor_position_queries(data)
    if had_cpr:
        _debug_log(debug_file, f"PTY_DATA: Filtered cursor position queries/reports ({len(data)} -> {len(filtered_data)} bytes)")
    
    if not filtered_data:
        _debug_log(debug_file, "PTY_DATA: All data was filtered out, skipping")
        return
    
    decoded_data = filtered_data.decode("utf-8", "replace")
    
    # Log a sample of the data for debugging
    sample = decoded_data[:100].replace('\n', '\\n').replace('\r', '\\r')
    _debug_log(debug_file, f"PTY_DATA: Sample: {repr(sample)}")
    
    # Feed ALL data at once to pyte - chunking was causing issues
    feed_start = time.time()
    pyte_stream.feed(decoded_data)
    feed_duration = time.time() - feed_start
    
    _debug_log(debug_file, f"PTY_DATA: Fed {len(decoded_data)} chars to pyte in {feed_duration:.6f}s")
    
    # Invalidate once after all data is processed
    if not menu_is_active[0]:
        app.invalidate()
    
    total_duration = time.time() - start_time
    _debug_log(debug_file, f"PTY_DATA: Total processing time {total_duration:.6f}s")


async def _read_from_pty(app, pty_fd, pyte_stream, log_queue, menu_is_active, raw_log_file, debug_file=None):
    """
    Reads data from the PTY in a loop using a thread pool executor.

    This approach uses `run_in_executor` to run the blocking `os.read` call in a
    separate thread, preventing it from blocking the main asyncio event loop. This
    is a robust pattern for integrating blocking I/O with asyncio.
    """
    _debug_log(debug_file, "PTY_READER: Starting read loop")
    
    # Check if data is already available before the first read
    # This helps diagnose the 31-second delay issue
    try:
        ready, _, _ = select.select([pty_fd], [], [], 0)
        if ready:
            _debug_log(debug_file, "PTY_READER: Data already available before first read")
        else:
            _debug_log(debug_file, "PTY_READER: No data available yet, will block on first read")
    except Exception as e:
        _debug_log(debug_file, f"PTY_READER: select() error: {e}")
    
    loop = asyncio.get_running_loop()
    read_count = 0
    try:
        while True:
            # Run the blocking os.read in the default thread pool executor
            read_start = time.time()
            data = await loop.run_in_executor(None, os.read, pty_fd, 4096)
            read_duration = time.time() - read_start
            read_count += 1

            if not data:
                # PTY closed, exit the reading loop
                _debug_log(debug_file, f"PTY_READER: PTY closed after {read_count} reads")
                break

            _debug_log(debug_file, f"PTY_READER: Read #{read_count}: {len(data)} bytes in {read_duration:.6f}s")

            # Log raw data if a log file is configured
            if raw_log_file:
                try:
                    raw_log_file.write(data)
                    raw_log_file.flush()
                except OSError:
                    pass  # Ignore errors if the file is closed or invalid

            # Process the data for display
            process_start = time.time()
            _process_pty_data(data, app, pyte_stream, menu_is_active, debug_file)
            process_duration = time.time() - process_start
            _debug_log(debug_file, f"PTY_READER: Processed data in {process_duration:.6f}s")

    except OSError as e:
        # This can happen if the PTY is closed while we are waiting to read,
        # for example, during a clean shutdown.
        _debug_log(debug_file, f"PTY_READER: OSError after {read_count} reads: {e}")
    except Exception as e:
        # Log any other unexpected errors and exit the loop
        _debug_log(debug_file, f"PTY_READER: Unexpected error after {read_count} reads: {e}")
        await _log_task_error(log_queue, "PTY READER", "CRASHED")


def _process_monitor_data(data, app, monitor_pyte_stream, menu_is_active, debug_file=None):
    """Feeds monitor data to the stream and invalidates the app."""
    _debug_log(debug_file, f"MONITOR_DATA: Processing {len(data)} bytes")
    monitor_pyte_stream.feed(data.decode("utf-8", "replace"))
    if not menu_is_active[0]:
        app.invalidate()


async def _read_from_monitor(app, monitor_pyte_stream, monitor_sock, log_queue, menu_is_active, debug_file=None):
    """Reads data from the QEMU monitor socket and feeds it to the monitor pyte stream."""
    if not monitor_sock:
        _debug_log(debug_file, "MONITOR_READER: No monitor socket, exiting")
        return
    _debug_log(debug_file, "MONITOR_READER: Starting read loop")
    loop = asyncio.get_running_loop()
    read_count = 0
    while True:
        try:
            data = await loop.sock_recv(monitor_sock, 4096)
            read_count += 1
            if not data:
                _debug_log(debug_file, f"MONITOR_READER: Connection closed after {read_count} reads")
                return  # Monitor connection closed, but don't exit app
            _debug_log(debug_file, f"MONITOR_READER: Read #{read_count}: {len(data)} bytes")
            _process_monitor_data(data, app, monitor_pyte_stream, menu_is_active, debug_file)
        except Exception as e:
            _debug_log(debug_file, f"MONITOR_READER: Error after {read_count} reads: {e}")
            await _log_task_error(log_queue, "MONITOR", "ERROR")
            return


async def _log_merger(log_queue, log_buffer, app):
    """Merges log messages from a queue into the log buffer."""
    while True:
        try:
            text = await log_queue.get()
            _append_to_log(log_buffer, text)
            app.invalidate()
            log_queue.task_done()
        except asyncio.CancelledError:
            break


def _cancel_async_tasks(tasks):
    """Cancels all provided asyncio tasks."""
    for task in tasks:
        task.cancel()


def _close_resources(pty_fd, monitor_sock, debug_file=None):
    """Closes the PTY file descriptor and monitor socket."""
    _debug_log(debug_file, "CLEANUP: Closing resources")
    if pty_fd != -1:
        os.close(pty_fd)
    if monitor_sock:
        monitor_sock.close()


def _terminate_qemu_process(qemu_process, debug_file=None):
    """Waits for the QEMU process to exit, killing it if necessary."""
    _debug_log(debug_file, "CLEANUP: Terminating QEMU process")
    try:
        qemu_process.wait(timeout=2)
        _debug_log(debug_file, f"CLEANUP: QEMU exited with code {qemu_process.returncode}")
    except subprocess.TimeoutExpired:
        _debug_log(debug_file, "CLEANUP: QEMU didn't exit, killing it")
        qemu_process.kill()
        qemu_process.wait()


def _cleanup_console(qemu_process, tasks, pty_fd, monitor_sock, attr_log_file, raw_log_file, debug_file=None):
    """Cancels all running async tasks and closes open resources."""
    _cancel_async_tasks(tasks)
    _close_resources(pty_fd, monitor_sock, debug_file)
    if attr_log_file:
        attr_log_file.close()
    if raw_log_file:
        raw_log_file.close()
    _terminate_qemu_process(qemu_process, debug_file)


def _get_verified_terminal_size(debug_file=None):
    """
    Gets and verifies the terminal size, with extensive logging.
    
    Returns: (cols, rows) tuple with validated dimensions
    """
    # Try ioctl first as it's the most reliable
    try:
        winsize = fcntl.ioctl(sys.stdout.fileno(), termios.TIOCGWINSZ, b'\x00' * 8)
        rows, cols = struct.unpack('HHHH', winsize)[:2]
        _debug_log(debug_file, f"TERMSIZE: ioctl(TIOCGWINSZ) returned rows={rows}, cols={cols}")
        
        if rows > 0 and cols > 0:
            # Sanity check the values
            if rows < 20 or rows > 200:
                _debug_log(debug_file, f"TERMSIZE: WARNING: Unusual row count {rows}, clamping to range [24, 100]")
                rows = max(24, min(rows, 100))
            
            if cols < 40 or cols > 500:
                _debug_log(debug_file, f"TERMSIZE: WARNING: Unusual column count {cols}, clamping to range [80, 200]")
                cols = max(80, min(cols, 200))
            
            _debug_log(debug_file, f"TERMSIZE: Final verified size: rows={rows}, cols={cols}")
            return cols, rows
    except Exception as e:
        _debug_log(debug_file, f"TERMSIZE: ioctl(TIOCGWINSZ) failed: {e}")
    
    # Fallback to shutil
    try:
        size = shutil.get_terminal_size(fallback=(80, 24))
        cols, rows = size.columns, size.lines
        _debug_log(debug_file, f"TERMSIZE: shutil.get_terminal_size() returned rows={rows}, cols={cols}")
        _debug_log(debug_file, f"TERMSIZE: Final size: rows={rows}, cols={cols}")
        return cols, rows
    except Exception as e:
        _debug_log(debug_file, f"TERMSIZE: shutil.get_terminal_size() failed: {e}")
    
    # Ultimate fallback
    _debug_log(debug_file, "TERMSIZE: Using fallback 80x24")
    return 80, 24


def _initialize_console_state(debug_file=None):
    """Initializes all stateful components for the console session."""
    _debug_log(debug_file, "INIT: Initializing console state")
    
    log_queue = Queue()
    log_buffer = Buffer(read_only=True)
    current_mode = [
        app_config.MODE_SERIAL_CONSOLE
    ]  # List to be mutable from closures
    menu_is_active = [False]  # List to be mutable from closures

    # Get verified terminal size with extensive logging
    cols, rows = _get_verified_terminal_size(debug_file)
    
    _debug_log(debug_file, f"INIT: Creating pyte screen with cols={cols}, rows={rows}")

    pyte_screen = pyte.Screen(cols, rows)
    pyte_stream = pyte.Stream(pyte_screen)
    monitor_pyte_screen = pyte.Screen(cols, rows)
    monitor_pyte_stream = pyte.Stream(monitor_pyte_screen)

    return log_queue, log_buffer, current_mode, menu_is_active, pyte_screen, pyte_stream, monitor_pyte_screen, monitor_pyte_stream


def _fix_termios_state(debug_file=None):
    """
    Fixes corrupted termios state on stdin before prompt_toolkit starts.
    Also hardens the state by disabling flow control and extended input processing.

    This addresses:
    1. A bug where termios.tcgetattr() returns bytes in the cc array.
    2. Potential kernel-level consumption of keystrokes due to flow control (IXON/IXOFF)
       or extended input processing (IEXTEN).
    """
    _debug_log(debug_file, "TERMIOS: Checking and fixing terminal state")
    try:
        # Get current terminal attributes
        attrs = termios.tcgetattr(sys.stdin.fileno())

        # attrs is a list: [iflag, oflag, cflag, lflag, ispeed, ospeed, cc]
        iflag = attrs[0]
        lflag = attrs[3]
        cc = attrs[6]

        needs_fix = False

        # 1. Fix corrupted CC array (bytes instead of ints)
        fixed_cc = []
        for item in cc:
            if isinstance(item, bytes):
                # Convert bytes to integer (take first byte if non-empty, else 0)
                fixed_value = item[0] if len(item) > 0 else 0
                fixed_cc.append(fixed_value)
                needs_fix = True
            elif isinstance(item, int):
                # Already an integer, keep as-is
                fixed_cc.append(item)
            else:
                # Unexpected type, convert to int if possible
                try:
                    fixed_cc.append(int(item))
                    needs_fix = True
                except (ValueError, TypeError):
                    # If conversion fails, use 0 as a safe default
                    fixed_cc.append(0)
                    needs_fix = True

        if needs_fix:
             attrs[6] = fixed_cc
             print("Fixed corrupted terminal state (cc array contained bytes instead of integers)", file=sys.stderr)
             _debug_log(debug_file, "TERMIOS: Fixed corrupted cc array")

        # 2. Harden Termios: Disable signals, flow control, and extended input.
        # ISIG: When enabled, Ctrl-C (INTR) and others generate signals. Disabling
        #       this allows prompt-toolkit to handle them as raw key presses.
        # IXON/IXOFF: Software flow control (Ctrl-S/Ctrl-Q).
        # IEXTEN: Implementation-defined input processing.
        # We disable these to ensure raw input reaches the application.
        new_iflag = iflag & ~(termios.IXON | termios.IXOFF)
        new_lflag = lflag & ~(termios.ISIG | termios.IEXTEN)

        if new_iflag != iflag or new_lflag != lflag:
            attrs[0] = new_iflag
            attrs[3] = new_lflag
            needs_fix = True
            print("Hardening termios: Disabled ISIG, IXON, IXOFF, IEXTEN", file=sys.stderr)
            _debug_log(debug_file, "TERMIOS: Hardened terminal state (disabled ISIG, IXON, IXOFF, IEXTEN)")

        # Apply changes if needed
        if needs_fix:
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSANOW, attrs)
            _debug_log(debug_file, "TERMIOS: Applied terminal state changes")
        else:
            _debug_log(debug_file, "TERMIOS: Terminal state is already correct")

    except Exception as e:
        # If we can't fix the terminal state, log the error but continue
        print(f"Warning: Could not fix/harden terminal state: {e}", file=sys.stderr)
        _debug_log(debug_file, f"TERMIOS: Error fixing terminal state: {e}")


def _setup_console_app(
    current_mode,
    pty_fd,
    monitor_sock,
    log_buffer,
    pyte_screen,
    monitor_pyte_screen,
    menu_is_active,
    attr_log_file,
    debug_file=None,
):
    """Creates and configures the prompt_toolkit Application."""
    _debug_log(debug_file, "APP: Setting up console application")

    def get_pty_screen_fragments():
        """
        Wrapper to pass pyte_screen to the fragment generator.
        Also checks for terminal resizing and updates pyte/PTY accordingly.
        """
        # Check if the host terminal size has changed
        cols, rows = _get_verified_terminal_size(debug_file)
        if pyte_screen.columns != cols or pyte_screen.lines != rows:
            _debug_log(debug_file, f"APP: Terminal resized from {pyte_screen.lines}x{pyte_screen.columns} to {rows}x{cols}")
            # Resize the pyte screen to match the host
            pyte_screen.resize(lines=rows, columns=cols)
            # Inform the guest OS of the new size via ioctl
            _set_pty_size(pty_fd, rows, cols, debug_file)

        return _get_pty_screen_fragments(pyte_screen, attr_log_file, debug_file)

    def get_monitor_screen_fragments():
        """Wrapper to pass monitor_pyte_screen to the fragment generator."""
        # We also resize the monitor screen for consistency, though it's less critical
        cols, rows = _get_verified_terminal_size(debug_file)
        if monitor_pyte_screen.columns != cols or monitor_pyte_screen.lines != rows:
            monitor_pyte_screen.resize(lines=rows, columns=cols)

        return _get_pty_screen_fragments(monitor_pyte_screen, attr_log_file, debug_file)

    key_bindings = _create_key_bindings(
        current_mode, pty_fd, monitor_sock, menu_is_active, debug_file
    )
    layout = _create_console_layout(
        get_pty_screen_fragments,
        get_monitor_screen_fragments,
        current_mode,
        pyte_screen,
        monitor_pyte_screen,
    )

    app = Application(
        layout=layout,
        key_bindings=key_bindings,
        full_screen=True,
        mouse_support=True,
    )
    _debug_log(debug_file, "APP: Console application setup complete")
    return app


def _create_async_tasks(app, pty_fd, pyte_stream, log_queue, monitor_sock, log_buffer, monitor_pyte_stream, menu_is_active, raw_log_file, debug_file=None):
    """Creates and returns all background asyncio tasks."""
    _debug_log(debug_file, "TASKS: Creating async tasks")
    pty_reader_task = asyncio.create_task(
        _read_from_pty(app, pty_fd, pyte_stream, log_queue, menu_is_active, raw_log_file, debug_file)
    )
    monitor_reader_task = asyncio.create_task(
        _read_from_monitor(app, monitor_pyte_stream, monitor_sock, log_queue, menu_is_active, debug_file)
    )
    merger_task = asyncio.create_task(_log_merger(log_queue, log_buffer, app))
    _debug_log(debug_file, "TASKS: Created 3 async tasks (PTY reader, monitor reader, log merger)")
    return [pty_reader_task, monitor_reader_task, merger_task]


async def _run_application_loop(app, pty_device, current_mode, pty_fd, debug_file=None):
    """Runs the main loop of the prompt_toolkit application, handling startup and shutdown."""
    print(
        f"\nConnected to serial console: {pty_device}\nPress Ctrl-] for control menu.\n",
        flush=True,
    )
    _debug_log(debug_file, "APP_LOOP: Starting prompt_toolkit application")

    result = await app.run_async() or ""
    _debug_log(debug_file, f"APP_LOOP: Application exited with result: {result}")
    print(f"Exiting console session: {result}")


async def _manage_console_session(
    app,
    pty_device,
    current_mode,
    pty_fd,
    qemu_process,
    tasks,
    monitor_sock,
    attr_log_file,
    raw_log_file,
    debug_file=None,
):
    """Runs the application loop, handles errors, and performs cleanup."""
    return_code = 0
    try:
        await _run_application_loop(app, pty_device, current_mode, pty_fd, debug_file)
    except Exception as e:
        print(f"Error running prompt_toolkit application: {e}", file=sys.stderr)
        _debug_log(debug_file, f"SESSION: Application error: {e}")
        return_code = 1
    finally:
        _cleanup_console(qemu_process, tasks, pty_fd, monitor_sock, attr_log_file, raw_log_file, debug_file)
        return_code = qemu_process.returncode or 0
        _debug_log(debug_file, f"SESSION: Exiting with return code {return_code}")

    return return_code


async def run_prompt_toolkit_console(qemu_process, pty_device, monitor_socket_path, debug_info=None):
    """Manages a prompt_toolkit-based text console session for QEMU."""
    debug_file = None
    if app_config.DEBUG_FILE:
        try:
            debug_path = Path(app_config.DEBUG_FILE)
            debug_path.parent.mkdir(parents=True, exist_ok=True)
            debug_file = open(debug_path, "w", encoding="utf-8", buffering=1)
            _debug_log(debug_file, "=== DEBUG SESSION STARTED ===")
            
            # Log timing information from process.py if available
            if debug_info:
                _debug_log(debug_file, f"TIMING: QEMU started at {debug_info.get('qemu_start', 0):.6f}")
                _debug_log(debug_file, f"TIMING: PTY found at {debug_info.get('pty_found', 0):.6f}")
                _debug_log(debug_file, f"TIMING: Console starting at {debug_info.get('console_start', 0):.6f}")
                if 'qemu_start' in debug_info and 'console_start' in debug_info:
                    delay = debug_info['console_start'] - debug_info['qemu_start']
                    _debug_log(debug_file, f"TIMING: Console started {delay:.3f}s after QEMU")
        except (OSError, IOError) as e:
            print(
                f"Error: Could not open debug file '{app_config.DEBUG_FILE}': {e}",
                file=sys.stderr,
            )
            return 1

    (
        log_queue,
        log_buffer,
        current_mode,
        menu_is_active,
        pyte_screen,
        pyte_stream,
        monitor_pyte_screen,
        monitor_pyte_stream,
    ) = _initialize_console_state(debug_file)

    attr_log_file = None
    raw_log_file = None
    if app_config.ATTRIBUTE_LOG_FILE:
        try:
            # Ensure the directory exists
            log_path = Path(app_config.ATTRIBUTE_LOG_FILE)
            raw_log_path = log_path.with_suffix(log_path.suffix + ".raw")
            log_path.parent.mkdir(parents=True, exist_ok=True)

            # Open attribute log file with line buffering
            attr_log_file = open(log_path, "w", encoding="utf-8", buffering=1)

            # Open raw log file in binary mode with no buffering
            raw_log_file = open(raw_log_path, "wb", buffering=0)

        except (OSError, IOError) as e:
            print(
                f"Error: Could not open log file(s) for '{app_config.ATTRIBUTE_LOG_FILE}': {e}",
                file=sys.stderr,
            )
            if attr_log_file:
                attr_log_file.close()
            if debug_file:
                debug_file.close()
            return 1

    try:
        pty_fd = _open_pty_device(pty_device, debug_file)
    except FileNotFoundError:
        if attr_log_file:
            attr_log_file.close()
        if raw_log_file:
            raw_log_file.close()
        if debug_file:
            debug_file.close()
        return 1

    # Fix any corrupted termios state before starting prompt_toolkit
    # This prevents the first keystroke from being consumed during error recovery
    _fix_termios_state(debug_file)

    # Set the initial PTY size to match the current terminal size.
    # This ensures the guest OS (e.g., GRUB, Linux) formats its output correctly from boot.
    _set_pty_size(pty_fd, pyte_screen.lines, pyte_screen.columns, debug_file)

    monitor_sock = await _connect_to_monitor(monitor_socket_path, debug_file)

    app = _setup_console_app(
        current_mode,
        pty_fd,
        monitor_sock,
        log_buffer,
        pyte_screen,
        monitor_pyte_screen,
        menu_is_active,
        attr_log_file,
        debug_file,
    )

    tasks = _create_async_tasks(
        app, pty_fd, pyte_stream, log_queue, monitor_sock, log_buffer, monitor_pyte_stream, menu_is_active, raw_log_file, debug_file
    )

    return_code = await _manage_console_session(
        app,
        pty_device,
        current_mode,
        pty_fd,
        qemu_process,
        tasks,
        monitor_sock,
        attr_log_file,
        raw_log_file,
        debug_file,
    )

    if debug_file:
        _debug_log(debug_file, "=== DEBUG SESSION ENDED ===")
        debug_file.close()

    return return_code
