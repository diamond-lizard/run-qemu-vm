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
    A processor that handles ANSI escape sequences and preserves newlines correctly.
    Processes only new text appended to the buffer for performance.
    """

    def __init__(self):
        self._parsed_fragments = []
        self._last_raw_length = 0

    def apply_transformation(
        self, transformation_input: TransformationInput
    ) -> Transformation:
        """
        Parses only the new text appended to the buffer since the last run,
        ensuring newlines are correctly preserved as line breaks.
        """
        full_text = transformation_input.document.text
        current_length = len(full_text)

        # Invalidate cache if buffer was cleared or shortened
        if current_length < self._last_raw_length:
            self._parsed_fragments = []
            self._last_raw_length = 0

        # Process only the new text
        new_raw_text = full_text[self._last_raw_length :]
        if new_raw_text:
            new_fragments = []
            lines = new_raw_text.split("\n")

            for i, line in enumerate(lines):
                # Process each line for ANSI codes (even if empty)
                if line:
                    line_fragments = to_formatted_text(ANSI(line))
                    new_fragments.extend(line_fragments)

                # Add explicit newline fragment after every line except the last
                if i < len(lines) - 1:
                    new_fragments.append(("", "\n"))

            self._parsed_fragments.extend(new_fragments)
            self._last_raw_length = current_length

        return Transformation(self._parsed_fragments)


async def run_prompt_toolkit_console(qemu_process, pty_device, monitor_socket_path):
    """Manages a prompt_toolkit-based text console session for QEMU."""
    log_queue = Queue()
    log_buffer = Buffer(read_only=True)
    current_mode = [
        app_config.MODE_SERIAL_CONSOLE
    ]  # Use a list to allow modification from closures

    # --- Pyte Terminal Emulation State ---
    pyte_screen = pyte.Screen(80, 24)
    pyte_stream = pyte.ByteStream(pyte_screen)

    def get_pty_screen_fragments():
        """Converts the pyte screen state into prompt_toolkit fragments."""
        fragments = []
        for y in range(pyte_screen.lines):
            line_fragments = []
            current_text = ""
            current_style = ""  # Start with an empty style string
            for x in range(pyte_screen.columns):
                char = pyte_screen.buffer[y, x]
                style_parts = []

                # Foreground color
                fg = char.get("fg", "default")
                if fg.startswith("#"):
                    style_parts.append(fg)
                elif fg != "default":
                    style_parts.append(f"fg:ansi{fg}")

                # Background color
                bg = char.get("bg", "default")
                if bg.startswith("#"):
                    style_parts.append(f"bg:{bg}")
                elif bg != "default":
                    style_parts.append(f"bg:ansi{bg}")

                # Attributes
                if char.get("bold"):
                    style_parts.append("bold")
                if char.get("italics"):
                    style_parts.append("italic")
                if char.get("underline"):
                    style_parts.append("underline")
                if char.get("reverse"):
                    style_parts.append("reverse")

                style_str = " ".join(style_parts)

                # The `pyte.Char` object is `char['data']`. Its `data` attribute holds the character.
                char_data = char["data"].data

                if style_str == current_style:
                    current_text += char_data
                else:
                    if current_text:
                        line_fragments.append((current_style, current_text))
                    current_style = style_str
                    current_text = char_data

            if current_text:
                line_fragments.append((current_style, current_text))

            fragments.extend(line_fragments)
            fragments.append(("", "\n"))
        return fragments

    def append_to_log(text_to_append: str):
        """Appends text to the log buffer, bypassing the read-only protection."""
        current_doc = log_buffer.document
        new_text = current_doc.text + text_to_append
        new_doc = Document(text=new_text, cursor_position=len(new_text))
        log_buffer.set_document(new_doc, bypass_readonly=True)

    try:
        pty_fd = os.open(pty_device, os.O_RDWR | os.O_NOCTTY)
    except FileNotFoundError:
        print(f"Error: PTY device '{pty_device}' not found.", file=sys.stderr)
        return 1

    monitor_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    monitor_sock.setblocking(False)
    try:
        await asyncio.get_running_loop().sock_connect(monitor_sock, monitor_socket_path)
    except (FileNotFoundError, ConnectionRefusedError):
        print("Warning: Could not connect to QEMU monitor.", file=sys.stderr)
        monitor_sock = None  # Disable monitor functionality

    key_bindings = KeyBindings()

    @key_bindings.add("c-]", eager=True)
    async def _(event):
        app = event.app
        result = await button_dialog(
            title="Control Menu",
            text="Select an action:",
            buttons=[
                ("Resume Console", "resume"),
                ("Enter Monitor", "monitor"),
                ("Quit QEMU", "quit"),
            ],
        ).run_async()

        if result == "quit":
            if monitor_sock:
                try:
                    monitor_sock.send(b"quit\n")
                    await asyncio.sleep(0.1)
                except OSError:
                    pass  # Socket might be closed
            app.exit(result="User quit")
        elif result == "monitor":
            current_mode[0] = app_config.MODE_QEMU_MONITOR
            await log_queue.put(
                "\n--- Switched to QEMU Monitor (Ctrl-] for menu) ---\n"
            )
            app.invalidate()
        else:  # resume
            current_mode[0] = app_config.MODE_SERIAL_CONSOLE
            app.invalidate()

    @key_bindings.add("<any>")
    async def _(event):
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

    is_serial_mode = Condition(lambda: current_mode[0] == app_config.MODE_SERIAL_CONSOLE)

    pty_control = FormattedTextControl(text=get_pty_screen_fragments)
    pty_container = ConditionalContainer(
        Window(content=pty_control, dont_extend_height=True), filter=is_serial_mode
    )

    log_control = BufferControl(buffer=log_buffer, input_processors=[AnsiColorProcessor()])
    monitor_container = ConditionalContainer(
        Window(content=log_control, wrap_lines=True), filter=~is_serial_mode
    )

    layout = Layout(HSplit([pty_container, monitor_container]))
    app = Application(layout=layout, key_bindings=key_bindings, full_screen=True)

    async def read_from_pty():
        loop = asyncio.get_running_loop()
        while True:
            try:
                data = await loop.run_in_executor(None, os.read, pty_fd, 4096)
                if not data:
                    app.exit(result="PTY closed")
                    return
                pyte_stream.feed(data)
                app.invalidate()
            except Exception as e:
                # Gracefully handle expected I/O error on shutdown when QEMU quits.
                if isinstance(e, OSError) and e.errno == 5:
                    break

                # For any other exception, log it as a crash.
                tb_str = traceback.format_exc()
                await log_queue.put(f"\n--- PTY READER CRASHED ---\n{tb_str}")
                # Don't exit, allow monitor to be used for diagnostics.
                return

    async def read_from_monitor():
        if not monitor_sock:
            return
        loop = asyncio.get_running_loop()
        while True:
            try:
                data = await loop.sock_recv(monitor_sock, 4096)
                if not data:
                    return  # Monitor connection closed, but don't exit app
                text = data.decode("utf-8", errors="replace")
                await log_queue.put(text)
            except Exception:
                tb_str = traceback.format_exc()
                await log_queue.put(f"\n--- MONITOR ERROR ---\n{tb_str}")
                return

    async def log_merger():
        """The single writer task that safely merges logs into the buffer."""
        while True:
            try:
                text = await log_queue.get()
                append_to_log(text)
                app.invalidate()
                log_queue.task_done()
            except asyncio.CancelledError:
                break

    pty_reader_task = asyncio.create_task(read_from_pty())
    monitor_reader_task = asyncio.create_task(read_from_monitor())
    merger_task = asyncio.create_task(log_merger())

    return_code = 0
    try:
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
    except Exception as e:
        print(f"Error running prompt_toolkit application: {e}", file=sys.stderr)
        return_code = 1
    finally:
        pty_reader_task.cancel()
        monitor_reader_task.cancel()
        merger_task.cancel()
        if pty_fd != -1:
            os.close(pty_fd)
        if monitor_sock:
            monitor_sock.close()

        try:
            qemu_process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            qemu_process.kill()
            qemu_process.wait()

        return_code = qemu_process.returncode or 0

    return return_code
