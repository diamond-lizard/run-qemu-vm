import asyncio
import atexit
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
from functools import lru_cache
from pathlib import Path

import pyte
from prompt_toolkit.application import Application
from prompt_toolkit.data_structures import Point
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.document import Document
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import ANSI, to_formatted_text
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout.containers import ConditionalContainer, HSplit, VSplit, Window, FloatContainer, Float
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.layout import Layout
from prompt_toolkit.layout.processors import (
    Processor,
    Transformation,
    TransformationInput,
)
from prompt_toolkit.styles import Style

from . import config as app_config


def _debug_log(debug_file, message):
    """Writes a timestamped debug message to the debug file if enabled."""
    if debug_file:
        try:
            timestamp = time.time()
            debug_file.write(f"[{timestamp:.6f}] {message}\n")
            debug_file.flush()
        except (ValueError, OSError):
            # File might be closed if called from atexit, ignore silently
            pass


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


@lru_cache(maxsize=4096)
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

    Optimized with Run-Length Encoding (RLE) to group adjacent characters
    with identical styles, significantly reducing rendering overhead.
    """
    line_fragments = []
    # pyte_screen.display contains the rendered text, including ACS translations.
    display_line = pyte_screen.display[y]
    buffer_line = pyte_screen.buffer[y]
    width = pyte_screen.columns

    current_style = None
    current_text_parts = []

    for x in range(width):
        # Get the character's style information from the buffer.
        char_style_info = buffer_line[x]

        # Get the character's data from the display property.
        # Pad with spaces if the display line is shorter than the screen width,
        # as .display can have ragged right edges.
        char_data = display_line[x] if x < len(display_line) else " "

        style_str = _get_char_style(char_style_info)

        if style_str != current_style:
            # Style changed: flush the previous group
            if current_style is not None:
                line_fragments.append((current_style, "".join(current_text_parts)))

            # Start new group
            current_style = style_str
            current_text_parts = [char_data]
        else:
            # Style same: accumulate character
            current_text_parts.append(char_data)

    # Flush the final group
    if current_style is not None:
        line_fragments.append((current_style, "".join(current_text_parts)))

    return line_fragments


def _get_pty_screen_fragments(pyte_screen, source_name, attr_log_file=None, debug_file=None):
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
            # New, simpler logging logic.
            # This logs the raw text content of the line directly from pyte's display buffer.
            # This is less detailed (no styles) but more robust for debugging.
            line_text = pyte_screen.display[y]
            if line_text.strip():
                attr_log_file.write(f"[{source_name}] Line {y:2d}: {repr(line_text)}\n")
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


class ControlMenuState:
    """Holds the state for the control menu dialog."""
    def __init__(self):
        self.is_visible = False
        self.result = None


def _create_custom_control_menu(state, current_mode, monitor_sock, debug_file=None):
    """
    Creates a custom control menu dialog using basic prompt_toolkit containers.

    This builds the dialog manually using Window, HSplit, and VSplit
    instead of relying on the Dialog widget or Frame, which may not be available
    or don't work properly in a Float container.
    """

    # Title bar
    title_text = "╔══ Control Menu ══╗"
    title_window = Window(
        content=FormattedTextControl(text=title_text),
        height=1,
        dont_extend_height=True,
        style="class:dialog.title",
    )

    # Menu body text
    body_text = "│ Select an action: │"
    body_window = Window(
        content=FormattedTextControl(text=body_text),
        height=1,
        dont_extend_height=True,
        style="class:dialog.body",
    )

    # Separator
    separator_text = "├────────────────────┤"
    separator_window = Window(
        content=FormattedTextControl(text=separator_text),
        height=1,
        dont_extend_height=True,
        style="class:dialog.body",
    )

    # Create button text with visual indicators
    def get_button_text(label, key):
        """Returns formatted text for a button."""
        return f"│ ({key}) {label:<15} │"

    resume_text = get_button_text("Resume Console", "R")
    monitor_text = get_button_text("Enter Monitor", "E")
    quit_text = get_button_text("Quit QEMU", "Q")

    # Create button windows
    resume_button = Window(
        content=FormattedTextControl(text=resume_text),
        height=1,
        dont_extend_height=True,
        style="class:dialog.button",
    )

    monitor_button = Window(
        content=FormattedTextControl(text=monitor_text),
        height=1,
        dont_extend_height=True,
        style="class:dialog.button",
    )

    quit_button = Window(
        content=FormattedTextControl(text=quit_text),
        height=1,
        dont_extend_height=True,
        style="class:dialog.button",
    )

    # Bottom border
    bottom_text = "╚════════════════════╝"
    bottom_window = Window(
        content=FormattedTextControl(text=bottom_text),
        height=1,
        dont_extend_height=True,
        style="class:dialog.body",
    )

    # Combine all parts vertically
    dialog_content = HSplit(
        [
            title_window,
            body_window,
            separator_window,
            resume_button,
            monitor_button,
            quit_button,
            bottom_window,
        ]
    )

    return dialog_content


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


async def _handle_resume_action(app, current_mode, pty_fd, pty_reader_callback, debug_file=None):
    """Handles the 'resume' action from the control menu."""
    _debug_log(debug_file, "MENU: User selected resume console")

    # Resume PTY reader by adding it back to the event loop
    loop = asyncio.get_running_loop()
    try:
        loop.add_reader(pty_fd, pty_reader_callback)
        _debug_log(debug_file, "MENU: PTY reader resumed")
    except Exception as e:
        _debug_log(debug_file, f"MENU: Error resuming PTY reader: {e}")

    current_mode[0] = app_config.MODE_SERIAL_CONSOLE
    app.renderer.reset()
    app.invalidate()


async def _process_control_menu_result(
    result, app, current_mode, monitor_sock, pty_fd, pty_reader_callback, debug_file=None
):
    """Handles the action selected from the control menu."""
    _debug_log(debug_file, f"MENU: Processing menu result: {result}")
    if result == "quit":
        await _handle_quit_action(app, monitor_sock, debug_file)
    elif result == "monitor":
        await _handle_monitor_action(app, current_mode, monitor_sock, debug_file)
    else:  # resume
        await _handle_resume_action(app, current_mode, pty_fd, pty_reader_callback, debug_file)


def _show_control_menu(app, menu_state, pty_fd, debug_file=None):
    """Shows the control menu dialog and pauses the PTY reader."""
    _debug_log(debug_file, f"MENU: Showing control menu (app id: {id(app)})")

    # Pause PTY reader by removing it from the event loop
    loop = asyncio.get_running_loop()
    try:
        loop.remove_reader(pty_fd)
        _debug_log(debug_file, "MENU: PTY reader paused")
    except Exception as e:
        # It might already be removed, which is fine.
        _debug_log(debug_file, f"MENU: Info removing PTY reader (might be normal): {e}")

    menu_state.is_visible = True
    menu_state.result = None
    app.invalidate()
    _debug_log(debug_file, "MENU: Control menu shown and invalidated")


def _hide_control_menu(app, menu_state, main_content_container, debug_file=None):
    """Hides the control menu dialog and returns focus to main content."""
    _debug_log(debug_file, "MENU: Hiding control menu")
    menu_state.is_visible = False

    # Return focus to the main content
    try:
        app.layout.focus(main_content_container)
        app.invalidate()
        _debug_log(debug_file, "MENU: Control menu hidden, focus returned to main content")
    except Exception as e:
        _debug_log(debug_file, f"MENU: Error hiding menu: {e}")


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


def _create_key_bindings(current_mode, pty_fd, monitor_sock, menu_state, main_content_container, debug_file=None):
    """Creates and configures the key bindings for the console application."""
    key_bindings = KeyBindings()

    # Create a condition for when the menu is NOT active
    menu_not_active = Condition(lambda: not menu_state.is_visible)
    menu_is_active = Condition(lambda: menu_state.is_visible)

    # Eagerly bind Ctrl-] to open the control menu (only when menu is not already active)
    @key_bindings.add("c-]", eager=True, filter=menu_not_active)
    def show_menu(event):
        _debug_log(debug_file, f"KEYBIND: Ctrl-] pressed (app id: {id(event.app)})")
        _show_control_menu(event.app, menu_state, pty_fd, debug_file)

    # Bind 'r' to resume when menu is active
    @key_bindings.add("r", filter=menu_is_active, eager=True)
    @key_bindings.add("R", filter=menu_is_active, eager=True)
    def menu_resume(event):
        _debug_log(debug_file, "KEYBIND: 'r' pressed in menu")
        menu_state.result = "resume"
        _hide_control_menu(event.app, menu_state, main_content_container, debug_file)

    # Bind 'e' to enter monitor when menu is active
    @key_bindings.add("e", filter=menu_is_active, eager=True)
    @key_bindings.add("E", filter=menu_is_active, eager=True)
    def menu_monitor(event):
        _debug_log(debug_file, "KEYBIND: 'e' pressed in menu")
        menu_state.result = "monitor"
        _hide_control_menu(event.app, menu_state, main_content_container, debug_file)

    # Bind 'q' to quit when menu is active
    @key_bindings.add("q", filter=menu_is_active, eager=True)
    @key_bindings.add("Q", filter=menu_is_active, eager=True)
    def menu_quit(event):
        _debug_log(debug_file, "KEYBIND: 'q' pressed in menu")
        menu_state.result = "quit"
        _hide_control_menu(event.app, menu_state, main_content_container, debug_file)

    # Eagerly bind Ctrl-C to forward it to the guest (only when menu is not active)
    @key_bindings.add("c-c", eager=True, filter=menu_not_active)
    async def forward_ctrl_c(event):
        await _forward_input(event, current_mode, pty_fd, monitor_sock, debug_file)

    # The <any> binding is a non-eager fallback (only when menu is not active)
    @key_bindings.add("<any>", filter=menu_not_active)
    async def forward_any(event):
        await _forward_input(event, current_mode, pty_fd, monitor_sock, debug_file)

    return key_bindings


def _create_console_layout(
    get_pty_screen_fragments,
    get_monitor_screen_fragments,
    current_mode,
    pyte_screen,
    monitor_pyte_screen,
    menu_state,
    debug_file=None,
):
    """Creates the prompt_toolkit layout for the console with integrated control menu."""
    is_serial_mode = Condition(lambda: current_mode[0] == app_config.MODE_SERIAL_CONSOLE)
    is_menu_visible = Condition(lambda: menu_state.is_visible)

    def get_pty_cursor_position():
        """Gets the cursor position from the PTY pyte screen."""
        x = max(0, min(pyte_screen.cursor.x, pyte_screen.columns - 1))
        y = max(0, min(pyte_screen.cursor.y, pyte_screen.lines - 1))
        return Point(x=x, y=y)

    def get_pty_fragments_with_debug():
        """Wrapper that logs when fragments are being generated for PTY."""
        _debug_log(debug_file, f"LAYOUT: get_pty_screen_fragments called (mode={current_mode[0]}, menu_visible={menu_state.is_visible})")
        result = get_pty_screen_fragments()
        _debug_log(debug_file, f"LAYOUT: get_pty_screen_fragments returned {len(result)} fragments")
        return result

    pty_control = FormattedTextControl(
        text=get_pty_fragments_with_debug, get_cursor_position=get_pty_cursor_position
    )
    pty_container = ConditionalContainer(
        Window(content=pty_control, dont_extend_height=True), filter=is_serial_mode
    )

    def get_monitor_cursor_position():
        """Gets the cursor position from the monitor pyte screen."""
        x = max(0, min(monitor_pyte_screen.cursor.x, monitor_pyte_screen.columns - 1))
        y = max(0, min(monitor_pyte_screen.cursor.y, monitor_pyte_screen.lines - 1))
        return Point(x=x, y=y)

    def get_monitor_fragments_with_debug():
        """Wrapper that logs when fragments are being generated for monitor."""
        _debug_log(debug_file, f"LAYOUT: get_monitor_screen_fragments called (mode={current_mode[0]}, menu_visible={menu_state.is_visible})")
        result = get_monitor_screen_fragments()
        _debug_log(debug_file, f"LAYOUT: get_monitor_screen_fragments returned {len(result)} fragments")
        return result

    monitor_control = FormattedTextControl(
        text=get_monitor_fragments_with_debug,
        get_cursor_position=get_monitor_cursor_position,
    )
    monitor_container = ConditionalContainer(
        Window(content=monitor_control, dont_extend_height=True),
        filter=~is_serial_mode,
    )

    # Main content container
    main_content = HSplit([pty_container, monitor_container])

    # Create the custom control menu dialog using basic containers
    control_menu_dialog = _create_custom_control_menu(menu_state, current_mode, None, debug_file)

    # Wrap the control menu in a ConditionalContainer to handle visibility
    conditional_menu_dialog = ConditionalContainer(
        content=control_menu_dialog,
        filter=is_menu_visible,
    )

    # Wrap main content in a FloatContainer with the control menu as a float
    root_container = FloatContainer(
        content=main_content,
        floats=[
            Float(
                content=conditional_menu_dialog,
            )
        ]
    )

    return Layout(root_container), main_content


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


def _process_pty_data(data, app, pyte_stream, menu_state, debug_file=None):
    """
    Feeds PTY data to the pyte stream and triggers UI invalidation.
    """
    start_time = time.time()
    _debug_log(debug_file, f"PTY_DATA: Processing {len(data)} bytes (menu_visible={menu_state.is_visible}, app id: {id(app)})")

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

    # Feed ALL data at once to pyte
    feed_start = time.time()
    pyte_stream.feed(decoded_data)
    feed_duration = time.time() - feed_start

    _debug_log(debug_file, f"PTY_DATA: Fed {len(decoded_data)} chars to pyte in {feed_duration:.6f}s")

    # Always invalidate - the layout will handle whether to show the menu or console
    _debug_log(debug_file, f"PTY_DATA: Calling app.invalidate()")
    app.invalidate()
    _debug_log(debug_file, f"PTY_DATA: app.invalidate() returned")

    total_duration = time.time() - start_time
    _debug_log(debug_file, f"PTY_DATA: Total processing time {total_duration:.6f}s")


def _create_pty_reader_callback(app, pty_fd, pyte_stream, menu_state, raw_log_file, debug_file=None):
    """
    Creates a callback function for the asyncio event loop to handle PTY data.

    This callback reads available data from the PTY, processes it, and handles
    the end-of-file condition when QEMU exits.
    """
    read_count = 0

    def _pty_reader_callback():
        """The actual callback passed to loop.add_reader()."""
        nonlocal read_count
        try:
            # Read data from the PTY file descriptor. This is non-blocking because
            # the event loop only calls us when data is ready.
            data = os.read(pty_fd, 4096)
            read_count += 1

            if not data:
                # PTY closed, QEMU has exited
                _debug_log(debug_file, f"PTY_READER: PTY closed after {read_count} reads - QEMU has exited")
                # Stop listening before exiting
                asyncio.get_running_loop().remove_reader(pty_fd)
                # Exit the application cleanly
                app.exit(result="QEMU exited")
                return

            _debug_log(debug_file, f"PTY_READER: Read #{read_count}: {len(data)} bytes")

            # Log raw data if a log file is configured
            if raw_log_file:
                try:
                    raw_log_file.write(data)
                    raw_log_file.flush()
                except OSError:
                    pass  # Ignore errors if the file is closed or invalid

            # Process the data for display
            _process_pty_data(data, app, pyte_stream, menu_state, debug_file)

        except BlockingIOError:
            # This can happen if we read when there's no data. It's not an error.
            _debug_log(debug_file, "PTY_READER: BlockingIOError, no data to read.")
            pass
        except OSError as e:
            # This can happen if the PTY is closed while we are waiting to read.
            _debug_log(debug_file, f"PTY_READER: OSError after {read_count} reads: {e}")
            asyncio.get_running_loop().remove_reader(pty_fd)
            app.exit(result="PTY error")
        except Exception:
            # Log any other unexpected errors and exit
            tb_str = traceback.format_exc()
            _debug_log(debug_file, f"PTY_READER: CRASHED after {read_count} reads:\n{tb_str}")
            asyncio.get_running_loop().remove_reader(pty_fd)
            app.exit(result="PTY reader crashed")

    return _pty_reader_callback


def _process_monitor_data(data, app, monitor_pyte_stream, menu_state, debug_file=None):
    """Feeds monitor data to the stream and invalidates the app."""
    _debug_log(debug_file, f"MONITOR_DATA: Processing {len(data)} bytes")
    monitor_pyte_stream.feed(data.decode("utf-8", "replace"))
    app.invalidate()


async def _read_from_monitor(app, monitor_pyte_stream, monitor_sock, log_queue, menu_state, debug_file=None):
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
            _process_monitor_data(data, app, monitor_pyte_stream, menu_state, debug_file)
        except Exception as e:
            _debug_log(debug_file, f"MONITOR_READER: Error after {read_count} reads: {e}")
            await _log_task_error(log_queue, "MONITOR", "ERROR")
            return


async def _process_qemu_output_queue(app, output_queue, monitor_pyte_stream, debug_file=None):
    """Reads QEMU stdout lines from the queue and feeds them to the monitor screen."""
    _debug_log(debug_file, "OUTPUT_PROCESSOR: Starting")
    while True:
        try:
            # Process all available lines in the queue
            lines_processed = 0
            while not output_queue.empty():
                line = output_queue.get_nowait()
                monitor_pyte_stream.feed(line)
                lines_processed += 1

            if lines_processed > 0:
                app.invalidate()

            await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            break
        except Exception as e:
            _debug_log(debug_file, f"OUTPUT_PROCESSOR: Error: {e}")
            await asyncio.sleep(1)


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


async def _menu_result_processor(app, menu_state, current_mode, monitor_sock, pty_fd, pty_reader_callback, debug_file=None):
    """Background task that processes menu results when the menu is dismissed."""
    while True:
        try:
            # Wait a bit and check if menu was just dismissed with a result
            await asyncio.sleep(0.1)

            if not menu_state.is_visible and menu_state.result is not None:
                result = menu_state.result
                menu_state.result = None  # Clear the result

                _debug_log(debug_file, f"MENU_PROCESSOR: Processing result: {result}")
                await _process_control_menu_result(
                    result, app, current_mode, monitor_sock, pty_fd, pty_reader_callback, debug_file
                )

        except asyncio.CancelledError:
            break
        except Exception as e:
            _debug_log(debug_file, f"MENU_PROCESSOR: Error: {e}")


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
    menu_state = ControlMenuState()

    # Get verified terminal size with extensive logging
    cols, rows = _get_verified_terminal_size(debug_file)

    _debug_log(debug_file, f"INIT: Creating pyte screen with cols={cols}, rows={rows}")

    pyte_screen = pyte.Screen(cols, rows)
    pyte_stream = pyte.Stream(pyte_screen)
    monitor_pyte_screen = pyte.Screen(cols, rows)
    monitor_pyte_stream = pyte.Stream(monitor_pyte_screen)

    return log_queue, log_buffer, current_mode, menu_state, pyte_screen, pyte_stream, monitor_pyte_screen, monitor_pyte_stream


def _restore_terminal_state(original_attrs, debug_file=None):
    """
    Restores terminal state by selectively re-enabling flags that were disabled
    by _apply_termios_hardening (ISIG, IEXTEN, IXON, IXOFF).

    This function is idempotent and safe to call multiple times.
    Errors are logged to debug file only, without raising exceptions or printing to stderr.

    Args:
        original_attrs: Terminal attributes saved before hardening, or None if hardening was skipped
        debug_file: Optional debug file handle for logging
    """
    if original_attrs is None:
        _debug_log(debug_file, "TERMIOS_RESTORE: No original attributes to restore (hardening was skipped)")
        return

    _debug_log(debug_file, "TERMIOS_RESTORE: Starting terminal state restoration")
    try:
        # Get current terminal attributes
        current_attrs = termios.tcgetattr(sys.stdin.fileno())

        # Restore only the specific flags we modified:
        # Re-enable IXON and IXOFF in iflag (input flags)
        original_iflag = original_attrs[0]
        current_iflag = current_attrs[0]
        restored_iflag = current_iflag | (original_iflag & (termios.IXON | termios.IXOFF))

        # Re-enable ISIG and IEXTEN in lflag (local flags)
        original_lflag = original_attrs[3]
        current_lflag = current_attrs[3]
        restored_lflag = current_lflag | (original_lflag & (termios.ISIG | termios.IEXTEN))

        _debug_log(debug_file, f"TERMIOS_RESTORE: Current - iflag={current_iflag:08x}, lflag={current_lflag:08x}")
        _debug_log(debug_file, f"TERMIOS_RESTORE: Original - iflag={original_iflag:08x}, lflag={original_lflag:08x}")
        _debug_log(debug_file, f"TERMIOS_RESTORE: Restored - iflag={restored_iflag:08x}, lflag={restored_lflag:08x}")

        # Apply the restoration
        current_attrs[0] = restored_iflag
        current_attrs[3] = restored_lflag
        termios.tcsetattr(sys.stdin.fileno(), termios.TCSANOW, current_attrs)

        _debug_log(debug_file, "TERMIOS_RESTORE: Successfully restored terminal state")

    except Exception as e:
        # Log error silently without printing to stderr or raising
        _debug_log(debug_file, f"TERMIOS_RESTORE: Error restoring terminal state: {e}")
        _debug_log(debug_file, f"TERMIOS_RESTORE: Traceback: {traceback.format_exc()}")


def _apply_termios_hardening(debug_file=None):
    """
    Applies additional termios hardening on top of prompt_toolkit's raw mode.

    Returns the original terminal attributes before modification so they can be restored later.

    This function must be called AFTER the Application is created but BEFORE
    app.run_async() is called. It ensures that control characters like Ctrl-]
    are not intercepted by the kernel.

    Disables:
    - ISIG: Signal generation (Ctrl-C, Ctrl-Z, etc.)
    - IEXTEN: Extended input processing
    - IXON/IXOFF: Software flow control (Ctrl-S/Ctrl-Q)
    """
    _debug_log(debug_file, "TERMIOS_HARDEN: Applying additional hardening")
    try:
        # Get and save current terminal attributes BEFORE modification
        original_attrs = termios.tcgetattr(sys.stdin.fileno())

        # Make a copy to modify
        attrs = original_attrs[:]

        # attrs is a list: [iflag, oflag, cflag, lflag, ispeed, ospeed, cc]
        iflag = attrs[0]
        lflag = attrs[3]

        _debug_log(debug_file, f"TERMIOS_HARDEN: Before - iflag={iflag:08x}, lflag={lflag:08x}")

        # Disable additional flags that might interfere with control character capture
        new_iflag = iflag & ~(termios.IXON | termios.IXOFF)
        new_lflag = lflag & ~(termios.ISIG | termios.IEXTEN)

        attrs[0] = new_iflag
        attrs[3] = new_lflag
        termios.tcsetattr(sys.stdin.fileno(), termios.TCSANOW, attrs)

        _debug_log(debug_file, f"TERMIOS_HARDEN: After - iflag={new_iflag:08x}, lflag={new_lflag:08x}")
        _debug_log(debug_file, "TERMIOS_HARDEN: Applied hardening (disabled ISIG, IEXTEN, IXON, IXOFF)")

        # Verify the hardening was applied
        verify_attrs = termios.tcgetattr(sys.stdin.fileno())
        verify_iflag = verify_attrs[0]
        verify_lflag = verify_attrs[3]

        has_ixon = bool(verify_iflag & termios.IXON)
        has_ixoff = bool(verify_iflag & termios.IXOFF)
        has_isig = bool(verify_lflag & termios.ISIG)
        has_iexten = bool(verify_lflag & termios.IEXTEN)

        _debug_log(debug_file, f"TERMIOS_HARDEN: Verification - IXON={has_ixon}, IXOFF={has_ixoff}, ISIG={has_isig}, IEXTEN={has_iexten}")

        if has_ixon or has_ixoff or has_isig or has_iexten:
            _debug_log(debug_file, "TERMIOS_HARDEN: WARNING - Some flags are still enabled!")
        else:
            _debug_log(debug_file, "TERMIOS_HARDEN: Verification successful - all flags disabled")

        return original_attrs

    except Exception as e:
        # Log the error but don't fail - prompt_toolkit's raw mode should still work
        _debug_log(debug_file, f"TERMIOS_HARDEN: Error applying hardening: {e}")
        print(f"Warning: Could not apply additional terminal hardening: {e}", file=sys.stderr)
        return None


def _setup_console_app(
    current_mode,
    pty_fd,
    monitor_sock,
    log_buffer,
    pyte_screen,
    monitor_pyte_screen,
    menu_state,
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

        return _get_pty_screen_fragments(pyte_screen, "PTY", attr_log_file, debug_file)

    def get_monitor_screen_fragments():
        """Wrapper to pass monitor_pyte_screen to the fragment generator."""
        # We also resize the monitor screen for consistency, though it's less critical
        cols, rows = _get_verified_terminal_size(debug_file)
        if monitor_pyte_screen.columns != cols or monitor_pyte_screen.lines != rows:
            monitor_pyte_screen.resize(lines=rows, columns=cols)

        return _get_pty_screen_fragments(monitor_pyte_screen, "MONITOR", attr_log_file, debug_file)

    layout, main_content_container = _create_console_layout(
        get_pty_screen_fragments,
        get_monitor_screen_fragments,
        current_mode,
        pyte_screen,
        monitor_pyte_screen,
        menu_state,
        debug_file,
    )

    key_bindings = _create_key_bindings(
        current_mode, pty_fd, monitor_sock, menu_state, main_content_container, debug_file
    )

    # Define a simple style for the dialog to make it visually distinct
    dialog_style = Style.from_dict({
        "dialog.title": "bg:#0000aa #ffffff bold",
        "dialog.body":  "bg:#aaaaaa #000000",
        "dialog.button": "bg:#000000 #ffffff",
    })

    app = Application(
        layout=layout,
        key_bindings=key_bindings,
        full_screen=True,
        mouse_support=True,
        style=dialog_style,
    )
    _debug_log(debug_file, f"APP: Console application setup complete (app id: {id(app)})")

    # Apply termios hardening AFTER app is created but BEFORE run_async()
    # This ensures control characters like Ctrl-] are captured by the app
    original_attrs = _apply_termios_hardening(debug_file)

    return app, original_attrs


def _create_async_tasks(app, pty_fd, log_queue, monitor_sock, log_buffer, monitor_pyte_stream, menu_state, current_mode, pty_reader_callback, qemu_output_queue, debug_file=None):
    """Creates and returns all background asyncio tasks."""
    _debug_log(debug_file, "TASKS: Creating async tasks")

    monitor_reader_task = asyncio.create_task(
        _read_from_monitor(app, monitor_pyte_stream, monitor_sock, log_queue, menu_state, debug_file)
    )
    merger_task = asyncio.create_task(_log_merger(log_queue, log_buffer, app))
    menu_processor_task = asyncio.create_task(
        _menu_result_processor(app, menu_state, current_mode, monitor_sock, pty_fd, pty_reader_callback, debug_file)
    )
    output_processor_task = asyncio.create_task(
        _process_qemu_output_queue(app, qemu_output_queue, monitor_pyte_stream, debug_file)
    )

    _debug_log(debug_file, "TASKS: Created 4 async tasks (monitor reader, log merger, menu processor, output processor)")
    return [monitor_reader_task, merger_task, menu_processor_task, output_processor_task]


async def _run_application_loop(app, pty_device, current_mode, pty_fd, debug_file=None):
    """Runs the main loop of the prompt_toolkit application, handling startup and shutdown."""
    print(
        f"\nConnected to serial console: {pty_device}\nPress Ctrl-] for control menu.\n",
        flush=True,
    )
    _debug_log(debug_file, f"APP_LOOP: Starting prompt_toolkit application (app id: {id(app)})")

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
    original_attrs=None,
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
        # Unregister PTY reader before cleaning up other resources
        try:
            loop = asyncio.get_running_loop()
            loop.remove_reader(pty_fd)
            _debug_log(debug_file, "CLEANUP: PTY reader unregistered")
        except Exception:
            pass  # Ignore errors if already removed or loop is closing

        # Restore terminal state after prompt_toolkit cleanup
        _restore_terminal_state(original_attrs, debug_file)

        _cleanup_console(qemu_process, tasks, pty_fd, monitor_sock, attr_log_file, raw_log_file, debug_file)
        return_code = qemu_process.returncode or 0
        _debug_log(debug_file, f"SESSION: Exiting with return code {return_code}")

    return return_code


async def run_prompt_toolkit_console(qemu_process, pty_device, monitor_socket_path, qemu_output_queue, debug_info=None):
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
        menu_state,
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

    # Set the initial PTY size to match the current terminal size.
    # This ensures the guest OS (e.g., GRUB, Linux) formats its output correctly from boot.
    _set_pty_size(pty_fd, pyte_screen.lines, pyte_screen.columns, debug_file)

    monitor_sock = await _connect_to_monitor(monitor_socket_path, debug_file)

    app, original_attrs = _setup_console_app(
        current_mode,
        pty_fd,
        monitor_sock,
        log_buffer,
        pyte_screen,
        monitor_pyte_screen,
        menu_state,
        attr_log_file,
        debug_file,
    )

    # Register atexit handler to ensure terminal restoration on abnormal exit
    # This is idempotent and safe to call multiple times
    atexit.register(_restore_terminal_state, original_attrs, debug_file)

    # Create the PTY reader callback. This function will be registered and
    # deregistered from the event loop as needed.
    pty_reader_callback = _create_pty_reader_callback(
        app, pty_fd, pyte_stream, menu_state, raw_log_file, debug_file
    )

    tasks = _create_async_tasks(
        app, pty_fd, log_queue, monitor_sock, log_buffer, monitor_pyte_stream, menu_state, current_mode, pty_reader_callback, qemu_output_queue, debug_file
    )

    # Register the initial PTY reader with the event loop
    loop = asyncio.get_running_loop()
    loop.add_reader(pty_fd, pty_reader_callback)
    _debug_log(debug_file, "TASKS: PTY reader registered with event loop")

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
        original_attrs,
        debug_file,
    )

    if debug_file:
        _debug_log(debug_file, "=== DEBUG SESSION ENDED ===")
        debug_file.close()

    return return_code
