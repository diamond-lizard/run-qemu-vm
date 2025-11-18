import asyncio
import os
import socket
import subprocess
import sys
import termios
import traceback
import tty
from asyncio import Queue

import pyte
from prompt_toolkit.application import Application
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
from .acs_translator import ACSTranslator


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

    # Foreground color
    fg = char.fg
    if fg.startswith("#"):
        style_parts.append(fg)
    elif fg != "default":
        style_parts.append(f"fg:ansi{fg}")

    # Background color
    bg = char.bg
    if bg.startswith("#"):
        style_parts.append(f"bg:{bg}")
    elif bg != "default":
        style_parts.append(f"bg:ansi{bg}")

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
    """
    line_fragments = []
    current_text = ""
    current_style = ""  # Start with an empty style string

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

        # Coalesce characters with the same style into a single fragment.
        if style_str == current_style:
            current_text += char_data
        else:
            if current_text:
                line_fragments.append((current_style, current_text))
            current_text = char_data
            current_style = style_str

    # After the loop, add any remaining text as the final fragment.
    if current_text:
        line_fragments.append((current_style, current_text))

    return line_fragments


def _get_pty_screen_fragments(pyte_screen):
    """Converts the entire pyte screen state into prompt_toolkit fragments."""
    fragments = []
    for y in range(pyte_screen.lines):
        line_fragments = _process_line_to_fragments(y, pyte_screen)
        fragments.extend(line_fragments)
        fragments.append(("", "\n"))
    return fragments


def _append_to_log(log_buffer: Buffer, text_to_append: str):
    """Appends text to the log buffer, bypassing the read-only protection."""
    current_doc = log_buffer.document
    new_text = current_doc.text + text_to_append
    new_doc = Document(text=new_text, cursor_position=len(new_text))
    log_buffer.set_document(new_doc, bypass_readonly=True)


def _open_pty_device(pty_device):
    """Opens the PTY device file descriptor and sets it to non-blocking mode."""
    try:
        pty_fd = os.open(pty_device, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    except FileNotFoundError:
        print(f"Error: PTY device '{pty_device}' not found.", file=sys.stderr)
        raise

    return pty_fd


async def _connect_to_monitor(monitor_socket_path):
    """Establishes a non-blocking connection to the QEMU monitor socket."""
    monitor_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    monitor_sock.setblocking(False)
    try:
        await asyncio.get_running_loop().sock_connect(monitor_sock, monitor_socket_path)
        return monitor_sock
    except (FileNotFoundError, ConnectionRefusedError):
        print("Warning: Could not connect to QEMU monitor.", file=sys.stderr)
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


async def _handle_quit_action(app, monitor_sock):
    """Handles the 'quit' action from the control menu."""
    if monitor_sock:
        try:
            monitor_sock.send(b"quit\n")
            await asyncio.sleep(0.1)
        except OSError:
            pass  # Socket might be closed
    app.exit(result="User quit")


async def _handle_monitor_action(app, current_mode, monitor_sock):
    """Handles the 'monitor' action from the control menu."""
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


async def _handle_resume_action(app, current_mode):
    """Handles the 'resume' action from the control menu."""
    current_mode[0] = app_config.MODE_SERIAL_CONSOLE
    app.renderer.reset()
    app.invalidate()


async def _process_control_menu_choice(
    result, app, current_mode, monitor_sock
):
    """Handles the action selected from the control menu."""
    if result == "quit":
        await _handle_quit_action(app, monitor_sock)
    elif result == "monitor":
        await _handle_monitor_action(app, current_mode, monitor_sock)
    else:  # resume
        await _handle_resume_action(app, current_mode)


async def _handle_control_menu(app, current_mode, monitor_sock, menu_is_active):
    """Displays the control menu and handles the user's choice."""
    menu_is_active[0] = True
    try:
        result = await _show_control_menu()
        await _process_control_menu_choice(
            result, app, current_mode, monitor_sock
        )
    finally:
        menu_is_active[0] = False
        app.invalidate()


async def _forward_input(event, current_mode, pty_fd, monitor_sock):
    """Forwards user input to the PTY or monitor based on the current mode."""
    loop = asyncio.get_running_loop()
    try:
        if current_mode[0] == app_config.MODE_SERIAL_CONSOLE:
            await loop.run_in_executor(
                None, os.write, pty_fd, event.data.encode("utf-8", errors="replace")
            )
        elif current_mode[0] == app_config.MODE_QEMU_MONITOR and monitor_sock:
            await loop.sock_sendall(
                monitor_sock, event.data.encode("utf-8", errors="replace")
            )
    except OSError:
        pass  # Ignore write errors if PTY/socket is closed


def _create_key_bindings(current_mode, pty_fd, monitor_sock, menu_is_active):
    """Creates and configures the key bindings for the console application."""
    key_bindings = KeyBindings()

    @key_bindings.add("c-]", eager=True)
    async def _(event):
        await _handle_control_menu(event.app, current_mode, monitor_sock, menu_is_active)

    @key_bindings.add("<any>")
    async def _(event):
        await _forward_input(event, current_mode, pty_fd, monitor_sock)

    return key_bindings


def _create_console_layout(get_pty_screen_fragments, get_monitor_screen_fragments, current_mode):
    """Creates the prompt_toolkit layout for the console."""
    is_serial_mode = Condition(lambda: current_mode[0] == app_config.MODE_SERIAL_CONSOLE)

    pty_control = FormattedTextControl(text=get_pty_screen_fragments)
    pty_container = ConditionalContainer(
        Window(content=pty_control, dont_extend_height=True), filter=is_serial_mode
    )

    monitor_control = FormattedTextControl(text=get_monitor_screen_fragments)
    monitor_container = ConditionalContainer(
        Window(content=monitor_control, dont_extend_height=True), filter=~is_serial_mode
    )

    return Layout(HSplit([pty_container, monitor_container]))


async def _log_task_error(log_queue: Queue, task_name: str, error_type: str = "ERROR"):
    """Logs a traceback for a crashed asyncio task."""
    tb_str = traceback.format_exc()
    await log_queue.put(f"\n--- {task_name} {error_type} ---\n{tb_str}")


def _process_pty_data(data, app, pyte_stream, acs_translator, menu_is_active):
    """Translates ACS sequences, feeds PTY data to the stream, and invalidates the app."""
    # First, translate any DEC Special Graphics (ACS) sequences to Unicode.
    translated_data = acs_translator.translate(data)
    # Then, decode the (potentially modified) data and feed it to pyte.
    pyte_stream.feed(translated_data.decode("utf-8", "replace"))
    if not menu_is_active[0]:
        app.invalidate()


class PTYReader:
    """
    Manages reading from a PTY file descriptor using asyncio's add_reader mechanism.

    This class uses a callback-based approach with loop.add_reader() instead of
    run_in_executor() to avoid mixing threading with async I/O, which can cause
    event loop state issues.
    """

    def __init__(self, app, pty_fd, pyte_stream, log_queue, acs_translator, menu_is_active):
        self.app = app
        self.pty_fd = pty_fd
        self.pyte_stream = pyte_stream
        self.log_queue = log_queue
        self.acs_translator = acs_translator
        self.menu_is_active = menu_is_active
        self.loop = None
        self.reader_registered = False
        self.stop_event = asyncio.Event()

    def _read_callback(self):
        """
        Callback invoked by the event loop when the PTY has data available.

        This runs synchronously in the event loop thread, so it must not block.
        """
        try:
            # Read available data (non-blocking since PTY is O_NONBLOCK)
            data = os.read(self.pty_fd, 4096)

            if not data:
                # PTY closed
                self._unregister_reader()
                # Schedule app exit in the event loop
                self.loop.call_soon_threadsafe(self.app.exit, "PTY closed")
                return

            # Process the data
            _process_pty_data(data, self.app, self.pyte_stream, self.acs_translator, self.menu_is_active)

        except BlockingIOError:
            # No data available right now (shouldn't happen since we were notified, but handle it)
            pass
        except OSError as e:
            # Handle expected I/O error on shutdown when QEMU quits (errno 5 = EIO)
            if e.errno == 5:
                self._unregister_reader()
                self.stop_event.set()
            else:
                # Log unexpected errors
                self._unregister_reader()
                # Schedule error logging
                asyncio.create_task(self._log_error(e))
        except Exception as e:
            # Catch any other exceptions to prevent them from crashing the event loop
            self._unregister_reader()
            asyncio.create_task(self._log_error(e))

    def _unregister_reader(self):
        """Removes the reader callback from the event loop."""
        if self.reader_registered and self.loop:
            try:
                self.loop.remove_reader(self.pty_fd)
                self.reader_registered = False
            except Exception:
                pass  # Ignore errors during cleanup

    async def _log_error(self, exception):
        """Logs an exception that occurred during PTY reading."""
        await _log_task_error(self.log_queue, "PTY READER", "CRASHED")

    async def start(self):
        """Registers the PTY file descriptor with the event loop for reading."""
        self.loop = asyncio.get_running_loop()
        self.loop.add_reader(self.pty_fd, self._read_callback)
        self.reader_registered = True

        # Wait until stop is signaled
        await self.stop_event.wait()

    def stop(self):
        """Stops reading from the PTY and unregisters the reader."""
        self._unregister_reader()
        self.stop_event.set()


async def _read_from_pty(app, pty_fd, pyte_stream, log_queue, acs_translator, menu_is_active):
    """
    Reads data from the PTY using asyncio's add_reader mechanism.

    This function creates a PTYReader instance and starts it, which registers
    a callback with the event loop to be invoked whenever the PTY has data available.
    """
    reader = PTYReader(app, pty_fd, pyte_stream, log_queue, acs_translator, menu_is_active)
    try:
        await reader.start()
    finally:
        reader.stop()


def _process_monitor_data(data, app, monitor_pyte_stream, menu_is_active):
    """Feeds monitor data to the stream and invalidates the app."""
    monitor_pyte_stream.feed(data.decode("utf-8", "replace"))
    if not menu_is_active[0]:
        app.invalidate()


async def _read_from_monitor(app, monitor_pyte_stream, monitor_sock, log_queue, menu_is_active):
    """Reads data from the QEMU monitor socket and feeds it to the monitor pyte stream."""
    if not monitor_sock:
        return
    loop = asyncio.get_running_loop()
    while True:
        try:
            data = await loop.sock_recv(monitor_sock, 4096)
            if not data:
                return  # Monitor connection closed, but don't exit app
            _process_monitor_data(data, app, monitor_pyte_stream, menu_is_active)
        except Exception:
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


def _close_resources(pty_fd, monitor_sock):
    """Closes the PTY file descriptor and monitor socket."""
    if pty_fd != -1:
        os.close(pty_fd)
    if monitor_sock:
        monitor_sock.close()


def _terminate_qemu_process(qemu_process):
    """Waits for the QEMU process to exit, killing it if necessary."""
    try:
        qemu_process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        qemu_process.kill()
        qemu_process.wait()


def _cleanup_console(qemu_process, tasks, pty_fd, monitor_sock):
    """Cancels all running async tasks and closes open resources."""
    _cancel_async_tasks(tasks)
    _close_resources(pty_fd, monitor_sock)
    _terminate_qemu_process(qemu_process)


def _initialize_console_state():
    """Initializes all stateful components for the console session."""
    log_queue = Queue()
    log_buffer = Buffer(read_only=True)
    current_mode = [
        app_config.MODE_SERIAL_CONSOLE
    ]  # List to be mutable from closures
    menu_is_active = [False]  # List to be mutable from closures
    pyte_screen = pyte.Screen(80, 24)
    pyte_stream = pyte.Stream(pyte_screen)
    monitor_pyte_screen = pyte.Screen(80, 24)
    monitor_pyte_stream = pyte.Stream(monitor_pyte_screen)
    acs_translator = ACSTranslator()
    return log_queue, log_buffer, current_mode, menu_is_active, pyte_screen, pyte_stream, monitor_pyte_screen, monitor_pyte_stream, acs_translator


def _fix_termios_state():
    """
    Fixes corrupted termios state on stdin before prompt_toolkit starts.

    This addresses a bug where termios.tcgetattr() returns a cc (control characters)
    array containing bytes objects instead of integers, which causes prompt_toolkit
    to misinterpret control character mappings and consume the first keystroke
    during error recovery.

    This function:
    1. Reads the current terminal attributes
    2. Ensures the cc array contains proper integers
    3. Sets the corrected attributes back to stdin
    """
    try:
        # Get current terminal attributes
        attrs = termios.tcgetattr(sys.stdin.fileno())

        # attrs is a list: [iflag, oflag, cflag, lflag, ispeed, ospeed, cc]
        # The cc (control characters) array is at index 6
        cc = attrs[6]

        # Check if cc contains bytes objects instead of integers
        needs_fix = False
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

        # If we found and fixed corrupted data, apply the corrected attributes
        if needs_fix:
            attrs[6] = fixed_cc
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSANOW, attrs)
            print("Fixed corrupted terminal state (cc array contained bytes instead of integers)", file=sys.stderr)

    except Exception as e:
        # If we can't fix the terminal state, log the error but continue
        # The application might still work, or the first keystroke issue might occur
        print(f"Warning: Could not fix terminal state: {e}", file=sys.stderr)


def _setup_console_app(
    current_mode, pty_fd, monitor_sock, log_buffer, pyte_screen, monitor_pyte_screen, menu_is_active
):
    """Creates and configures the prompt_toolkit Application."""

    def get_pty_screen_fragments():
        """Wrapper to pass pyte_screen to the fragment generator."""
        return _get_pty_screen_fragments(pyte_screen)

    def get_monitor_screen_fragments():
        """Wrapper to pass monitor_pyte_screen to the fragment generator."""
        return _get_pty_screen_fragments(monitor_pyte_screen)

    key_bindings = _create_key_bindings(current_mode, pty_fd, monitor_sock, menu_is_active)
    layout = _create_console_layout(get_pty_screen_fragments, get_monitor_screen_fragments, current_mode)
    app = Application(layout=layout, key_bindings=key_bindings, full_screen=True)
    return app


def _create_async_tasks(app, pty_fd, pyte_stream, log_queue, monitor_sock, log_buffer, monitor_pyte_stream, acs_translator, menu_is_active):
    """Creates and returns all background asyncio tasks."""
    pty_reader_task = asyncio.create_task(
        _read_from_pty(app, pty_fd, pyte_stream, log_queue, acs_translator, menu_is_active)
    )
    monitor_reader_task = asyncio.create_task(
        _read_from_monitor(app, monitor_pyte_stream, monitor_sock, log_queue, menu_is_active)
    )
    merger_task = asyncio.create_task(_log_merger(log_queue, log_buffer, app))
    return [pty_reader_task, monitor_reader_task, merger_task]


async def _run_application_loop(app, pty_device, current_mode, pty_fd):
    """Runs the main loop of the prompt_toolkit application, handling startup and shutdown."""
    print(
        f"\nConnected to serial console: {pty_device}\nPress Ctrl-] for control menu.\n",
        flush=True,
    )

    result = await app.run_async() or ""
    print(f"Exiting console session: {result}")


async def _manage_console_session(
    app, pty_device, current_mode, pty_fd, qemu_process, tasks, monitor_sock
):
    """Runs the application loop, handles errors, and performs cleanup."""
    return_code = 0
    try:
        await _run_application_loop(app, pty_device, current_mode, pty_fd)
    except Exception as e:
        print(f"Error running prompt_toolkit application: {e}", file=sys.stderr)
        return_code = 1
    finally:
        _cleanup_console(qemu_process, tasks, pty_fd, monitor_sock)
        return_code = qemu_process.returncode or 0

    return return_code


async def run_prompt_toolkit_console(qemu_process, pty_device, monitor_socket_path):
    """Manages a prompt_toolkit-based text console session for QEMU."""
    (
        log_queue,
        log_buffer,
        current_mode,
        menu_is_active,
        pyte_screen,
        pyte_stream,
        monitor_pyte_screen,
        monitor_pyte_stream,
        acs_translator,
    ) = _initialize_console_state()

    try:
        pty_fd = _open_pty_device(pty_device)
    except FileNotFoundError:
        return 1

    monitor_sock = await _connect_to_monitor(monitor_socket_path)

    # Fix any corrupted termios state before starting prompt_toolkit
    # This prevents the first keystroke from being consumed during error recovery
    _fix_termios_state()

    app = _setup_console_app(
        current_mode, pty_fd, monitor_sock, log_buffer, pyte_screen, monitor_pyte_screen, menu_is_active
    )

    tasks = _create_async_tasks(
        app, pty_fd, pyte_stream, log_queue, monitor_sock, log_buffer, monitor_pyte_stream, acs_translator, menu_is_active
    )

    return await _manage_console_session(
        app, pty_device, current_mode, pty_fd, qemu_process, tasks, monitor_sock
    )
