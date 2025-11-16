import asyncio
import os
import socket
import subprocess
import sys
import traceback
from asyncio import Queue

import pyte
from prompt_toolkit.application import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.document import Document
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import ANSI, to_formatted_text
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout.containers import ConditionalContainer, HSplit, Window
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.layout import Layout
from prompt_toolkit.layout.processors import (
    Processor,
    Transformation,
    TransformationInput,
)
from prompt_toolkit.shortcuts import button_dialog

from . import config as app_config


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


def _coalesce_char_into_fragment(char, current_text, current_style, line_fragments):
    """
    Coalesces a character into a text run if its style matches.

    If the style differs, the current text run is added to the fragments list
    and a new run is started with the given character.

    Returns the updated text run and style.
    """
    style_str = _get_char_style(char)
    # Access the character data directly from the Char object
    char_data = char.data

    if style_str == current_style:
        current_text += char_data
    else:
        if current_text:
            line_fragments.append((current_style, current_text))
        current_text = char_data
        current_style = style_str

    return current_text, current_style


def _process_line_to_fragments(y, pyte_screen):
    """Processes a single line from the pyte screen into fragments."""
    line_fragments = []
    current_text = ""
    current_style = ""  # Start with an empty style string

    for x in range(pyte_screen.columns):
        # Use two-level indexing: buffer[y][x] returns a Char object
        # Note: buffer[y, x] (tuple indexing) returns StaticDefaultDict, which is incorrect
        char = pyte_screen.buffer[y][x]
        current_text, current_style = _coalesce_char_into_fragment(
            char, current_text, current_style, line_fragments
        )

    # After the loop, add any remaining text as the final fragment
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


def _sanitize_control_characters(text: str) -> str:
    """Removes all ASCII control characters except for Tab and Line Feed."""
    sanitized_chars = []
    for char in text:
        # Keep printable ASCII, Tab, and Line Feed
        if char >= " " or char in ("\t", "\n"):
            sanitized_chars.append(char)
    return "".join(sanitized_chars)


def _append_to_log(log_buffer: Buffer, text_to_append: str):
    """Appends text to the log buffer, bypassing the read-only protection."""
    current_doc = log_buffer.document
    new_text = current_doc.text + text_to_append
    new_doc = Document(text=new_text, cursor_position=len(new_text))
    log_buffer.set_document(new_doc, bypass_readonly=True)


def _open_pty_device(pty_device):
    """Opens the PTY device file descriptor, returning it or raising FileNotFoundError."""
    try:
        return os.open(pty_device, os.O_RDWR | os.O_NOCTTY)
    except FileNotFoundError:
        print(f"Error: PTY device '{pty_device}' not found.", file=sys.stderr)
        raise


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
    """Displays the control menu and returns the user's choice."""
    return await button_dialog(
        title="Control Menu",
        text="Select an action:",
        buttons=[
            ("Resume Console", "resume"),
            ("Enter Monitor", "monitor"),
            ("Quit QEMU", "quit"),
        ],
    ).run_async()


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


async def _handle_control_menu(app, current_mode, monitor_sock):
    """Displays the control menu and handles the user's choice."""
    result = await _show_control_menu()
    await _process_control_menu_choice(
        result, app, current_mode, monitor_sock
    )


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


def _create_key_bindings(current_mode, pty_fd, monitor_sock):
    """Creates and configures the key bindings for the console application."""
    key_bindings = KeyBindings()

    @key_bindings.add("c-]", eager=True)
    async def _(event):
        await _handle_control_menu(event.app, current_mode, monitor_sock)

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


def _process_pty_data(data, app, pyte_stream):
    """Feeds PTY data to the stream and invalidates the app."""
    pyte_stream.feed(data)
    app.invalidate()


async def _handle_pty_read_error(e, log_queue):
    """Handles exceptions during PTY read operations."""
    # Gracefully handle expected I/O error on shutdown when QEMU quits.
    if isinstance(e, OSError) and e.errno == 5:
        return True  # Indicates loop should break

    # For any other exception, log it as a crash.
    await _log_task_error(log_queue, "PTY READER", "CRASHED")
    # Don't exit, allow monitor to be used for diagnostics.
    return False  # Indicates function should return


async def _read_from_pty(app, pty_fd, pyte_stream, log_queue):
    """Reads data from the PTY, feeds it to the terminal emulator, and invalidates the app."""
    loop = asyncio.get_running_loop()
    while True:
        try:
            data = await loop.run_in_executor(None, os.read, pty_fd, 4096)
            if not data:
                app.exit(result="PTY closed")
                return
            _process_pty_data(data, app, pyte_stream)
        except Exception as e:
            should_break = await _handle_pty_read_error(e, log_queue)
            if should_break:
                break
            else:
                return


def _process_monitor_data(data, app, monitor_pyte_stream):
    """Feeds monitor data to the stream and invalidates the app."""
    monitor_pyte_stream.feed(data)
    app.invalidate()


async def _read_from_monitor(app, monitor_pyte_stream, monitor_sock, log_queue):
    """Reads data from the QEMU monitor socket and feeds it to the monitor pyte stream."""
    if not monitor_sock:
        return
    loop = asyncio.get_running_loop()
    while True:
        try:
            data = await loop.sock_recv(monitor_sock, 4096)
            if not data:
                return  # Monitor connection closed, but don't exit app
            _process_monitor_data(data, app, monitor_pyte_stream)
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
    pyte_screen = pyte.Screen(80, 24)
    pyte_stream = pyte.ByteStream(pyte_screen)
    monitor_pyte_screen = pyte.Screen(80, 24)
    monitor_pyte_stream = pyte.ByteStream(monitor_pyte_screen)
    return log_queue, log_buffer, current_mode, pyte_screen, pyte_stream, monitor_pyte_screen, monitor_pyte_stream


def _setup_console_app(
    current_mode, pty_fd, monitor_sock, log_buffer, pyte_screen, monitor_pyte_screen
):
    """Creates and configures the prompt_toolkit Application."""

    def get_pty_screen_fragments():
        """Wrapper to pass pyte_screen to the fragment generator."""
        return _get_pty_screen_fragments(pyte_screen)

    def get_monitor_screen_fragments():
        """Wrapper to pass monitor_pyte_screen to the fragment generator."""
        return _get_pty_screen_fragments(monitor_pyte_screen)

    key_bindings = _create_key_bindings(current_mode, pty_fd, monitor_sock)
    layout = _create_console_layout(get_pty_screen_fragments, get_monitor_screen_fragments, current_mode)
    app = Application(layout=layout, key_bindings=key_bindings, full_screen=True)
    return app


def _create_async_tasks(app, pty_fd, pyte_stream, log_queue, monitor_sock, log_buffer, monitor_pyte_stream):
    """Creates and returns all background asyncio tasks."""
    pty_reader_task = asyncio.create_task(
        _read_from_pty(app, pty_fd, pyte_stream, log_queue)
    )
    monitor_reader_task = asyncio.create_task(
        _read_from_monitor(app, monitor_pyte_stream, monitor_sock, log_queue)
    )
    merger_task = asyncio.create_task(_log_merger(log_queue, log_buffer, app))
    return [pty_reader_task, monitor_reader_task, merger_task]


async def _run_application_loop(app, pty_device, current_mode, pty_fd):
    """Runs the main loop of the prompt_toolkit application, handling startup and shutdown."""
    print(
        f"\nConnected to serial console: {pty_device}\nPress Ctrl-] for control menu.\n",
        flush=True,
    )
    # HACK: Send Ctrl-L after a delay to force a redraw in some guest OSes
    await asyncio.sleep(1.5)
    if current_mode[0] == app_config.MODE_SERIAL_CONSOLE:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, os.write, pty_fd, b"\x0c")

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
        pyte_screen,
        pyte_stream,
        monitor_pyte_screen,
        monitor_pyte_stream,
    ) = _initialize_console_state()

    try:
        pty_fd = _open_pty_device(pty_device)
    except FileNotFoundError:
        return 1

    monitor_sock = await _connect_to_monitor(monitor_socket_path)

    app = _setup_console_app(
        current_mode, pty_fd, monitor_sock, log_buffer, pyte_screen, monitor_pyte_screen
    )

    tasks = _create_async_tasks(
        app, pty_fd, pyte_stream, log_queue, monitor_sock, log_buffer, monitor_pyte_stream
    )

    return await _manage_console_session(
        app, pty_device, current_mode, pty_fd, qemu_process, tasks, monitor_sock
    )
