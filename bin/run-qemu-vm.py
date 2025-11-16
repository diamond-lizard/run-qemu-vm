#!/usr/bin/env python3

#
# Description:
#   This script launches a QEMU virtual machine to install or run an operating
#   system for a specified architecture (e.g., aarch64, x86_64). It intelligently
#   selects the required firmware (UEFI or BIOS) and allows the user to choose
#   between a graphical (GUI) or text-based (serial) console.
#
# Usage:
#   1. List available architectures:
#      ./run-qemu-vm.py --architecture list
#
#   2. UEFI installation with a GUI (AArch64):
#      ./run-qemu-vm.py --architecture aarch64 --disk-image my-os.qcow2 --cdrom uefi-installer.iso --console gui
#
#   3. UEFI installation with a text-only terminal (x86_64):
#      ./run-qemu-vm.py --architecture x86_64 --disk-image my-x86.qcow2 --cdrom alpine-virt.iso --console text
#      # Script forces BIOS boot for x86 text mode to use serial-friendly bootloaders.
#      # On Linux with --console text and --cdrom, it attempts automated direct kernel boot.
#      # Press Ctrl-] for control menu
#
#   4. Share a directory with the guest:
#      ./run-qemu-vm.py --architecture riscv64 --disk-image my-riscv.qcow2 --share-dir /path/on/host:sharename
#      # Inside guest: sudo mount -t 9p -o trans=virtio sharename /mnt/shared
#
#   5. To see all available options:
#      ./run-qemu-vm.py --help
#
# Prerequisites:
#   - QEMU must be installed (e.g., via `brew install qemu`).
#   - For automated UEFI boot on simple ISOs, '7z' must be in the PATH.
#   - For Linux direct kernel boot, 'isoinfo' (from genisoimage) is preferred,
#     with '7z' as a fallback.
#   - For directory sharing, guest kernel must have 9P support (CONFIG_9P_FS).
#

import argparse
import subprocess
import sys
import os
import shutil
import tempfile
from pathlib import Path
import re
import socket
import threading
import platform
import traceback

# Ensure the package is in the path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from run_qemu_vm import config as app_config

import asyncio
from asyncio import Queue
from collections import deque

from prompt_toolkit.application import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.document import Document
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout.containers import Window, ConditionalContainer, HSplit
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.layout import Layout
from prompt_toolkit.layout.processors import Processor, Transformation, TransformationInput
from prompt_toolkit.filters import Condition
from prompt_toolkit.shortcuts import button_dialog
from prompt_toolkit.formatted_text import ANSI, to_formatted_text
import pyte

def get_qemu_prefix(brew_executable):
    """Finds the Homebrew installation prefix for QEMU."""
    try:
        prefix = subprocess.check_output([brew_executable, "--prefix", "qemu"], text=True, stderr=subprocess.PIPE).strip()
        return Path(prefix)
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        print(f"Error: Could not find QEMU prefix using '{brew_executable}'. Is Homebrew installed? Error: {e}", file=sys.stderr)
        sys.exit(1)

def detect_firmware_type(iso_path, architecture):
    """Detects the firmware type for an ISO based on architecture and filename."""
    if not iso_path:
        return 'uefi' if architecture in ['aarch64', 'x86_64', 'riscv64'] else 'bios'
    if 'bios' in Path(iso_path).name.lower():
         return 'bios'
    return 'uefi' if architecture in ['aarch64', 'x86_64', 'riscv64'] else 'uefi'


def find_uefi_bootloader(seven_zip_executable, iso_path, architecture):
    """Inspects an ISO to find the UEFI bootloader file for the given architecture."""
    bootloader_patterns = {
        'aarch64': ['bootaa64.efi'],
        'x86_64': ['bootx64.efi'],
        'riscv64': ['bootriscv64.efi']
    }
    patterns = bootloader_patterns.get(architecture)
    if not patterns:
        return None, None
    print(f"Info: Searching for UEFI bootloader in '{iso_path}' for {architecture}...")

    tools_tried = []
    is_macos = platform.system() == 'Darwin'

    # Prefer isoinfo on Linux
    isoinfo_path = shutil.which("isoinfo")
    if not is_macos and isoinfo_path:
        try:
            cmd = [isoinfo_path, '-i', iso_path, '-J', '-find', f'*/{patterns[0]}']
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            if result.stdout.strip():
                path_line = result.stdout.split('\n', 1)[0].strip()
                if path_line:
                    bootloader_full_path = path_line
                    p = Path(bootloader_full_path)
                    print(f"Info: Found UEFI bootloader: {p.name} at path {bootloader_full_path} using isoinfo")
                    return p.name, str(p)
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            tools_tried.append(f"isoinfo ({e})")

    # Fallback to 7z if available
    try:
        result = subprocess.run([seven_zip_executable, 'l', iso_path], capture_output=True, text=True, check=True)
    except (FileNotFoundError, subprocess.CalledProcessError) as e:
        tools_tried.append(f"7z ({e})")
    else:
        for line in result.stdout.splitlines():
            for pattern in patterns:
                if pattern in line.lower():
                    parts = line.split()
                    if len(parts) > 0 and parts[-1].lower().endswith(pattern):
                        bootloader_full_path = parts[-1]
                        p = Path(bootloader_full_path)
                        print(f"Info: Found UEFI bootloader: {p.name} at path {bootloader_full_path} using 7z")
                        return p.name, str(p).replace('/', '\\')
        tools_tried.append("7z (no bootloader found)")

    # If we get here, no tool worked
    if not is_macos:
        print(f"Error: Could not extract from ISO. Tried: {', '.join(tools_tried)}", file=sys.stderr)
        print("       Install either genisoimage/isoinfo or p7zip-full/7z.", file=sys.stderr)
        sys.exit(1)
    else:
        print(f"Warning: Could not find a suitable bootloader ({', '.join(patterns)}) in the ISO. Tried: {', '.join(tools_tried)}", file=sys.stderr)
        return None, None


def find_kernel_and_initrd(seven_zip_executable, iso_path):
    """
    Inspects an ISO to find the paths to a Linux kernel and initial ramdisk
    by searching for common file names (vmlinuz, initrd/initramfs) using
    isoinfo (preferred) or 7z.

    Returns (kernel_path, initrd_path) if found, or (None, None) if not found.
    """
    found_kernel, found_initrd = None, None
    print(f"Info: Searching for kernel and initrd in '{iso_path}'...")

    # Initialize candidate lists for Linux detection
    candidate_kernels = []
    candidate_initrds = []

    if platform.system() == 'Linux':
        # Linux patterns including DVD root paths and flexible Debian support
        kernel_patterns = [
            r'/vmlinuz$',
            r'/install(\.amd)?/.*vmlinuz',
            r'/boot/vmlinuz',
            r'vmlinuz$',
            r'boot/linux$'
        ]
        initrd_patterns = [
            r'/initrd\.gz$',
            r'/install(\.amd)?/.*initrd\.gz',
            r'/boot/initrd\.gz',
            r'/boot/initramfs\.gz',
            r'initrd\.gz$'
        ]
    else:
        # Original macOS patterns unchanged
        kernel_patterns = [r'vmlinuz', r'/boot/vmlinuz', r'boot/linux']
        initrd_patterns = [r'initrd', r'initramfs', r'/boot/initrd', r'/boot/initramfs']

    tools_tried = []

    # --- Attempt 1: isoinfo (Linux preference) ---
    isoinfo_path = shutil.which("isoinfo")
    if isoinfo_path:
        try:
            result = subprocess.run([isoinfo_path, '-R', '-l', '-i', iso_path], capture_output=True, text=True, check=True, encoding='latin-1', errors='ignore')
            tools_tried.append("isoinfo")

            current_directory = None
            candidate_kernels = []
            candidate_initrds = []

            blocks = result.stdout.split("\n\n")
            for block in blocks:
                lines = [line.strip() for line in block.splitlines()]
                if not lines:
                    continue

                dir_header = next((line for line in lines if line.startswith("Directory listing of")), None)
                if dir_header:
                    current_directory = dir_header.split()[-1].strip("'").strip(":")

                if not current_directory:
                    continue

                for line in lines:
                    if not line.startswith('-r-'):
                        continue

                    parts = line.split()
                    if len(parts) < 10:
                        continue

                    try:
                        filename = parts[-1]
                        if filename in ('.', '..'):
                            continue

                        full_path = os.path.join('/', current_directory, filename)
                        full_path = full_path.replace('\\', '/').replace('//', '/')
                        lower_path = full_path.lower()

                        file_size = int(parts[4])
                        if file_size < 100_000:
                            continue

                        for pattern in kernel_patterns:
                            if re.search(pattern, lower_path):
                                candidate_kernels.append((full_path, file_size))
                                if os.environ.get("DEBUG"):
                                    print(f"Debug: Kernel candidate: {full_path} (size:{file_size})")
                                break

                        for pattern in initrd_patterns:
                            if re.search(pattern, lower_path):
                                candidate_initrds.append((full_path, file_size))
                                if os.environ.get("DEBUG"):
                                    print(f"Debug: Initrd candidate: {full_path} (size:{file_size})")
                                break

                    except (ValueError, IndexError) as e:
                        if os.environ.get("DEBUG"):
                            print(f"Debug: Skipping malformed ISO line: {line} ({e})")

            candidate_kernels.sort(key=lambda x: (x[0].count('/'), -x[1]))
            candidate_initrds.sort(key=lambda x: (x[0].count('/'), -x[1]))

            if platform.system() == 'Linux':
                if candidate_kernels and candidate_initrds:
                    found_kernel = candidate_kernels[0][0]
                    found_initrd = candidate_initrds[0][0]
                    print(f"Info: Best kernel candidate: {found_kernel}")
                    print(f"Info: Best initrd candidate: {found_initrd}")
                    return found_kernel, found_initrd
            else:
                if found_kernel and found_initrd:
                    print(f"Info: Found Kernel: {found_kernel} (using pattern matching)")
                    print(f"Info: Found Initrd: {found_initrd} (using pattern matching)")
                    return found_kernel.lstrip('/'), found_initrd.lstrip('/')
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            tools_tried[-1] = f"isoinfo failed ({e})"

    # --- Attempt 2: 7z fallback ---
    try:
        result = subprocess.run([seven_zip_executable, 'l', iso_path], capture_output=True, text=True, check=True)
        tools_tried.append("7z")

        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) > 0:
                full_path = parts[-1].lstrip('./').replace('\\', '/')
                lower_path = full_path.lower()

                if not found_kernel:
                    for pattern in kernel_patterns:
                        if pattern in lower_path and not lower_path.endswith('.mod') and lower_path.endswith(('bin', 'z', 'bimage', 'elf')):
                            found_kernel = full_path
                            break

                if not found_initrd:
                    for pattern in initrd_patterns:
                        if pattern in lower_path and lower_path.endswith(('.img', '.gz')):
                            found_initrd = full_path
                            break

                if found_kernel and found_initrd:
                    print(f"Info: Found Kernel: {found_kernel} (using 7z)")
                    print(f"Info: Found Initrd: {found_initrd} (using 7z)")
                    return found_kernel.lstrip('/'), found_initrd.lstrip('/')
    except (FileNotFoundError, subprocess.CalledProcessError) as e:
        tools_tried.append(f"7z failed ({e})")

    print(f"Warning: Could not find a suitable Linux kernel and initial ramdisk (initrd) in the ISO for direct boot. Tried: {', '.join(tools_tried)}", file=sys.stderr)
    print("Warning: Falling back to standard BIOS boot (direct kernel boot disabled).", file=sys.stderr)
    return None, None


def add_direct_kernel_boot_args(base_args, config, kernel_file, initrd_file):
    """
    Constructs QEMU arguments for direct kernel boot, injects the console argument,
    and executes QEMU via run_qemu.
    """
    args = list(base_args)

    # 1. Remove the existing -cdrom flag and its path, as we are replacing it
    # with -kernel and -initrd flags.
    if config["cdrom"] in args:
        cdrom_index = args.index("-cdrom")
        args.pop(cdrom_index)
        args.pop(cdrom_index)

    # 2. Add the direct boot arguments
    serial_profile_key = config.get("serial_device") or app_config.ARCH_DEFAULT_SERIAL.get(config['architecture'], app_config.ARCH_DEFAULT_SERIAL['default'])
    if serial_profile_key == 'virtio':
        kernel_cmd_line = "console=hvc0 panic=1"
    else:
        kernel_cmd_line = "console=ttyS0,115200n8 panic=1"

    if platform.system() == 'Linux':
        with tempfile.TemporaryDirectory(prefix="qemu-kernel-") as temp_dir:
            kernel_path = os.path.join(temp_dir, os.path.basename(kernel_file))
            initrd_path = os.path.join(temp_dir, os.path.basename(initrd_file))

            try:
                kernel_extract_path = kernel_file.lstrip('/')
                initrd_extract_path = initrd_file.lstrip('/')

                if os.environ.get("DEBUG"):
                    print(f"Debug: Extracting kernel from '{kernel_extract_path}' in ISO")
                    print(f"Debug: Extracting initrd from '{initrd_extract_path}' in ISO")

                result = subprocess.run(
                    [config["seven_zip_executable"], "e", config["cdrom"],
                     f"-o{temp_dir}", kernel_extract_path],
                    capture_output=True, text=True, check=False
                )

                if result.returncode != 0 or not os.path.exists(kernel_path):
                    print(f"Error: Failed to extract kernel '{kernel_extract_path}' from ISO.", file=sys.stderr)
                    if os.environ.get("DEBUG"):
                        print(f"Debug: 7z output: {result.stdout}", file=sys.stderr)
                        print(f"Debug: 7z errors: {result.stderr}", file=sys.stderr)
                    sys.exit(1)

                print(f"Info: Extracted kernel to: {kernel_path}")
                if os.environ.get("DEBUG"):
                    print(f"Debug: Kernel exists: {os.path.exists(kernel_path)}, size: {os.path.getsize(kernel_path)} bytes")

                result = subprocess.run(
                    [config["seven_zip_executable"], "e", config["cdrom"],
                     f"-o{temp_dir}", initrd_extract_path],
                    capture_output=True, text=True, check=False
                )

                if result.returncode != 0 or not os.path.exists(initrd_path):
                    print(f"Error: Failed to extract initrd '{initrd_extract_path}' from ISO.", file=sys.stderr)
                    if os.environ.get("DEBUG"):
                        print(f"Debug: 7z output: {result.stdout}", file=sys.stderr)
                        print(f"Debug: 7z errors: {result.stderr}", file=sys.stderr)
                    sys.exit(1)

                print(f"Info: Extracted initrd to: {initrd_path}")
                if os.environ.get("DEBUG"):
                    print(f"Debug: Initrd exists: {os.path.exists(initrd_path)}, size: {os.path.getsize(initrd_path)} bytes")

            except Exception as e:
                print(f"Error: Failed to extract files from ISO: {e}", file=sys.stderr)
                sys.exit(1)

            args.extend([
                "-kernel", kernel_path,
                "-initrd", initrd_path,
                "-append", kernel_cmd_line,
                "-nographic"
            ])

            print(f"Info: Direct Kernel Boot with append args: '{kernel_cmd_line}'")

            monitor_socket = f"/tmp/qemu-monitor-{os.getpid()}.sock"
            config['monitor_socket'] = monitor_socket
            args.extend(["-monitor", f"unix:{monitor_socket},server,nowait", "-chardev", "pty,id=char0"])

            serial_args = app_config.SERIAL_DEVICE_PROFILES[serial_profile_key]['args']
            print(f"Info: Using '{serial_profile_key}' serial profile for Direct Kernel Boot.")
            args.extend(serial_args)

            run_qemu(args, config)
            sys.exit(0)
    else:
        # Original macOS behavior
        iso_kernel_path = kernel_file.lstrip('/')
        iso_initrd_path = initrd_file.lstrip('/')
        args.extend([
            "-kernel", f"iso:{config['cdrom']}:{iso_kernel_path}",
            "-initrd", f"iso:{config['cdrom']}:{iso_initrd_path}",
            "-append", kernel_cmd_line,
            "-nographic"
        ])

        print(f"Info: Direct Kernel Boot with append args: '{kernel_cmd_line}'")

        monitor_socket = f"/tmp/qemu-monitor-{os.getpid()}.sock"
        config['monitor_socket'] = monitor_socket
        args.extend(["-monitor", f"unix:{monitor_socket},server,nowait", "-chardev", "pty,id=char0"])

        serial_args = app_config.SERIAL_DEVICE_PROFILES[serial_profile_key]['args']
        print(f"Info: Using '{serial_profile_key}' serial profile for Direct Kernel Boot.")
        args.extend(serial_args)

        run_qemu(args, config)
        sys.exit(0)


def parse_share_dir_argument(share_dir_arg):
    """Parse and validate the --share-dir argument."""
    if not share_dir_arg:
        return None, None
    if ':' not in share_dir_arg:
        print("Error: --share-dir format must be '/host/path:mount_tag'", file=sys.stderr)
        sys.exit(1)
    host_path, mount_tag = share_dir_arg.rsplit(':', 1)
    if not os.path.isdir(host_path):
        print(f"Error: Host path is not a directory: {host_path}", file=sys.stderr)
        sys.exit(1)
    if not re.match(app_config.MOUNT_TAG_PATTERN, mount_tag):
        print(f"Error: Invalid characters in mount tag '{mount_tag}'. Allowed: {app_config.MOUNT_TAG_ALLOWED_CHARS}", file=sys.stderr)
        sys.exit(1)
    return os.path.abspath(host_path), mount_tag


def create_and_run_uefi_with_automation(base_args, config):
    """Creates a temporary startup.nsh to automate UEFI boot and runs QEMU."""
    bootloader_name, bootloader_script_path = find_uefi_bootloader(config['seven_zip_executable'], config['cdrom'], config['architecture'])
    if bootloader_name and bootloader_script_path:
        with tempfile.TemporaryDirectory(prefix="qemu-uefi-boot-") as temp_dir:
            with open(Path(temp_dir) / "startup.nsh", "w") as f:
                f.write(f"# Auto-generated by run-qemu-vm.py\necho -off\necho 'Attempting to boot from CD-ROM...'\nFS0:\n{bootloader_script_path}\n")
            print(f"Info: Created temporary startup.nsh in '{temp_dir}'")
            automated_args = list(base_args)
            automated_args.extend(["-drive", f"if=none,id=boot-script,format=raw,file=fat:rw:{temp_dir}", "-device", "usb-storage,drive=boot-script"])
            run_qemu(automated_args, config)
    else:
        print("Warning: Proceeding without boot automation script.", file=sys.stderr)
        run_qemu(base_args, config)




class AnsiColorProcessor(Processor):
    """
    A processor that handles ANSI escape sequences and preserves newlines correctly.
    Processes only new text appended to the buffer for performance.
    """
    def __init__(self):
        self._parsed_fragments = []
        self._last_raw_length = 0

    def apply_transformation(self, transformation_input: TransformationInput) -> Transformation:
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
        new_raw_text = full_text[self._last_raw_length:]
        if new_raw_text:
            new_fragments = []
            lines = new_raw_text.split('\n')
            
            for i, line in enumerate(lines):
                # Process each line for ANSI codes (even if empty)
                if line:
                    line_fragments = to_formatted_text(ANSI(line))
                    new_fragments.extend(line_fragments)
                
                # Add explicit newline fragment after every line except the last
                if i < len(lines) - 1:
                    new_fragments.append(('', '\n'))

            self._parsed_fragments.extend(new_fragments)
            self._last_raw_length = current_length

        return Transformation(self._parsed_fragments)


async def run_prompt_toolkit_console(qemu_process, pty_device, monitor_socket_path):
    """Manages a prompt_toolkit-based text console session for QEMU."""
    log_queue = Queue()
    log_buffer = Buffer(read_only=True)
    current_mode = [app_config.MODE_SERIAL_CONSOLE]  # Use a list to allow modification from closures

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
                fg = char.get('fg', 'default')
                if fg.startswith('#'):
                    style_parts.append(fg)
                elif fg != 'default':
                    style_parts.append(f"fg:ansi{fg}")

                # Background color
                bg = char.get('bg', 'default')
                if bg.startswith('#'):
                    style_parts.append(f"bg:{bg}")
                elif bg != 'default':
                    style_parts.append(f"bg:ansi{bg}")

                # Attributes
                if char.get('bold'): style_parts.append('bold')
                if char.get('italics'): style_parts.append('italic')
                if char.get('underline'): style_parts.append('underline')
                if char.get('reverse'): style_parts.append('reverse')

                style_str = " ".join(style_parts)

                # The `pyte.Char` object is `char['data']`. Its `data` attribute holds the character.
                char_data = char['data'].data

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
            fragments.append(('', '\n'))
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
        monitor_sock = None # Disable monitor functionality

    key_bindings = KeyBindings()

    @key_bindings.add('c-]', eager=True)
    async def _(event):
        app = event.app
        result = await button_dialog(
            title="Control Menu",
            text="Select an action:",
            buttons=[
                ('Resume Console', 'resume'),
                ('Enter Monitor', 'monitor'),
                ('Quit QEMU', 'quit')
            ]
        ).run_async()

        if result == 'quit':
            if monitor_sock:
                try:
                    monitor_sock.send(b'quit\n')
                    await asyncio.sleep(0.1)
                except OSError:
                    pass # Socket might be closed
            app.exit(result="User quit")
        elif result == 'monitor':
            current_mode[0] = app_config.MODE_QEMU_MONITOR
            await log_queue.put("\n--- Switched to QEMU Monitor (Ctrl-] for menu) ---\n")
            app.invalidate()
        else:  # resume
            current_mode[0] = app_config.MODE_SERIAL_CONSOLE
            app.invalidate()

    @key_bindings.add('<any>')
    async def _(event):
        loop = asyncio.get_running_loop()
        try:
            if current_mode[0] == app_config.MODE_SERIAL_CONSOLE:
                await loop.run_in_executor(None, os.write, pty_fd, event.data.encode('utf-8', errors='replace'))
            elif current_mode[0] == app_config.MODE_QEMU_MONITOR and monitor_sock:
                await loop.sock_sendall(monitor_sock, event.data.encode('utf-8', errors='replace'))
        except OSError:
            pass # Ignore write errors if PTY/socket is closed

    is_serial_mode = Condition(lambda: current_mode[0] == app_config.MODE_SERIAL_CONSOLE)

    pty_control = FormattedTextControl(text=get_pty_screen_fragments)
    pty_container = ConditionalContainer(Window(content=pty_control, dont_extend_height=True), filter=is_serial_mode)

    log_control = BufferControl(
        buffer=log_buffer,
        input_processors=[AnsiColorProcessor()]
    )
    monitor_container = ConditionalContainer(Window(content=log_control, wrap_lines=True), filter=~is_serial_mode)

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
                    return # Monitor connection closed, but don't exit app
                text = data.decode('utf-8', errors='replace')
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
        print(f"\nConnected to serial console: {pty_device}\nPress Ctrl-] for control menu.\n", flush=True)
        # HACK: Send Ctrl-L after a delay to force a redraw in some guest OSes
        await asyncio.sleep(1.5)
        if current_mode[0] == app_config.MODE_SERIAL_CONSOLE:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, os.write, pty_fd, b'\x0c')

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


def parse_pty_device_from_thread(process, event, result_holder):
    """Reads from process output in a thread, finds PTY device, and drains output."""
    pty_device_found = False
    for line in iter(process.stdout.readline, ''):
        sys.stdout.write(line)
        sys.stdout.flush()
        if not pty_device_found:
            match = re.search(r'char device redirected to (/dev/[^\s]+)', line)
            if match:
                result_holder[0] = match.group(1)
                print(f"Info: Found serial console device: {result_holder[0]}", flush=True)
                pty_device_found = True
                event.set()
    process.stdout.close()

def build_qemu_args(config):
    """Constructs the list of arguments for the QEMU command."""
    args = [
        config["qemu_executable"], "-M", config["machine_type"], "-accel", config["accelerator"],
        "-cpu", config["cpu_model"], "-m", config["memory"], "-smp", str(config["smp_cores"]),
        "-netdev", config["network_backend"], "-device", config["network_device"],
        "-hda", config["disk_image"], "-device", config["usb_controller"],
    ]
    if config["cdrom"]:
        args.extend(["-cdrom", config["cdrom"]])
    if config.get("share_dir"):
        host_path, mount_tag = parse_share_dir_argument(config["share_dir"])
        if host_path and mount_tag:
            args.extend(["-virtfs", f"local,path={host_path},mount_tag={mount_tag},security_model={app_config.VIRTFS_SECURITY_MODEL},id={mount_tag}"])
        else:
            sys.exit(1)

    # --- NEW: Direct Kernel Boot Attempt for text console (Linux only) ---
    is_linux = platform.system() == 'Linux'
    if is_linux and config['console'] == 'text' and config["cdrom"] and config['architecture'] in ['x86_64', 'aarch64', 'riscv64']:
        print("Info: Text console requested with CD-ROM on Linux. Attempting automated Direct Kernel Boot for reliable serial output.")
        kernel_file, initrd_file = find_kernel_and_initrd(config["seven_zip_executable"], config["cdrom"])

        if kernel_file and initrd_file:
            add_direct_kernel_boot_args(args, config, kernel_file, initrd_file)
            return
        else:
            print("Info: Direct kernel boot not available for this ISO. Continuing with standard boot.", file=sys.stderr)

    # --- Firmware Selection Logic ---
    use_uefi = None
    if config.get('firmware') == 'bios':
        use_uefi = False
        print("Info: User explicitly selected Legacy BIOS boot.")
    elif config.get('firmware') == 'uefi':
        use_uefi = True
        print("Info: User explicitly selected UEFI boot.")
    else:
        is_x86 = config['architecture'] in ['x86_64', 'i386']
        if is_x86 and config['console'] == 'text':
            use_uefi = False
            print("Info: Forcing Legacy BIOS for x86 text mode to find serial-friendly bootloader.")
        elif is_x86 and config['console'] == 'gui' and sys.platform == "darwin":
            use_uefi = False
            print("Info: Forcing Legacy BIOS for x86 GUI mode on macOS to avoid UEFI display errors.")
        else:
            use_uefi = True

    if use_uefi:
        if config.get('uefi_code') and os.path.exists(config['uefi_code']):
            print("Info: Using UEFI boot.")
            args.extend(["-drive", f"if=pflash,format=raw,readonly=on,file={config['uefi_code']}", "-drive", f"if=pflash,format=raw,file={config['uefi_vars']}"])
        else:
            print("Error: UEFI boot was selected, but UEFI firmware is not found.", file=sys.stderr)
            if config.get('uefi_code'):
                print(f"       Attempted path: {config['uefi_code']}", file=sys.stderr)
            print("       Please install QEMU's UEFI firmware files (e.g., 'edk2-qemu') or specify '--firmware bios'.", file=sys.stderr)
            sys.exit(1)
    else:
        print("Info: Using Legacy BIOS boot.")

    if config['console'] == 'text':
        monitor_socket = f"/tmp/qemu-monitor-{os.getpid()}.sock"
        config['monitor_socket'] = monitor_socket
        args.extend(["-monitor", f"unix:{monitor_socket},server,nowait", "-chardev", "pty,id=char0"])

        serial_profile_key = config.get("serial_device") or app_config.ARCH_DEFAULT_SERIAL.get(config['architecture'], app_config.ARCH_DEFAULT_SERIAL['default'])
        serial_args = app_config.SERIAL_DEVICE_PROFILES[serial_profile_key]['args']
        print(f"Info: Using '{serial_profile_key}' serial profile.")
        args.extend(serial_args)
        args.append("-nographic")
    else:
        vga_type = config.get('vga_type')
        if not vga_type:
            if config['architecture'] in ['x86_64', 'i386']:
                vga_type = 'std'
            elif config['architecture'] == 'aarch64':
                args.extend(["-device", "virtio-gpu-pci"])
                vga_type = 'none'
            else:
                vga_type = 'std'

        if vga_type != 'none':
            print(f"Info: Using VGA type '{vga_type}'.")
            args.extend(["-vga", vga_type])

        args.extend(["-display", config["display_type"], "-device", config["keyboard_device"], "-device", config["mouse_device"]])

    boot_order = 'd' if config.get("boot_from") == 'cdrom' or (not config.get("boot_from") and config["cdrom"]) else 'c'
    args.extend(["-boot", f"order={boot_order}"])

    if use_uefi and boot_order == 'd' and config["cdrom"]:
        create_and_run_uefi_with_automation(args, config)
    else:
        run_qemu(args, config)


def prepare_uefi_vars_file(vars_path, code_path):
    """Ensures the UEFI variables file is valid."""
    try:
        code_size = os.path.getsize(code_path)
        if not os.path.exists(vars_path) or os.path.getsize(vars_path) != code_size:
            print(f"Info: UEFI variables file missing or incorrect size. Creating/updating at: {vars_path}")
            shutil.copyfile(code_path, vars_path)
    except (FileNotFoundError, IOError) as e:
        print(f"Error handling UEFI files: {e}", file=sys.stderr)
        sys.exit(1)

def run_qemu(args, config):
    """Executes the QEMU command and handles text console if needed."""
    print("--- Starting QEMU with the following command ---", flush=True)
    formatted_command = f"{args[0]} \\\n"
    formatted_command += " \\\n".join([f"    {subprocess.list2cmdline([arg])}" for arg in args[1:]])
    print(formatted_command, flush=True)
    print("-" * 50, flush=True)

    try:
        if config.get('console') == 'text':
            process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
            event, holder = threading.Event(), [None]
            thread = threading.Thread(target=parse_pty_device_from_thread, args=(process, event, holder))
            thread.daemon = True
            thread.start()
            if not event.wait(timeout=10.0) or not holder[0]:
                print("Error: Could not find PTY device in QEMU output within 10 seconds.", file=sys.stderr)
                process.kill()
                thread.join()
                sys.exit(1)

            # Hand off to the prompt_toolkit console manager
            try:
                return_code = asyncio.run(run_prompt_toolkit_console(process, holder[0], config['monitor_socket']))
            except KeyboardInterrupt:
                # This is a fallback; prompt_toolkit should handle Ctrl-C gracefully.
                print("\nInterrupted by user.", flush=True)
                return_code = 130
            finally:
                thread.join()

            sys.exit(return_code)
        else:
            process = subprocess.Popen(args)
            process.wait()
            if process.returncode != 0:
                sys.exit(process.returncode)
    except FileNotFoundError:
        print(f"Error: QEMU executable '{args[0]}' not found.", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nInterrupted")
        sys.exit(130)

def main():
    """Parses command-line arguments and launches the VM."""
    parser = argparse.ArgumentParser(description="Launch a QEMU virtual machine with flexible options.", formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--architecture", required=True, help="QEMU system architecture (e.g., aarch64, x86_64). Use 'list' for options.")
    parser.add_argument("--disk-image", help="Path to the primary virtual hard disk image (.qcow2).")
    parser.add_argument("--cdrom", help="Path to a bootable ISO file.")
    parser.add_argument("--boot-from", choices=['cdrom', 'hd'], help="Specify boot device. Defaults to 'cdrom' if --cdrom is used.")
    parser.add_argument("--firmware", choices=['uefi', 'bios'], help="Force a specific firmware mode, overriding automatic selection.")
    parser.add_argument("--console", choices=['gui', 'text'], default='gui', help="Console type. Defaults to 'gui'.")
    parser.add_argument("--share-dir", metavar="/HOST/PATH:MOUNT_TAG", help="Share a host directory with the guest via VirtFS.")
    parser.add_argument("--serial-device", help="Serial device profile for text mode. Use 'list' to see options.")

    parser.add_argument("--machine-type", default=app_config.MACHINE_TYPE, help="QEMU machine type.")
    parser.add_argument("--accelerator", default=app_config.ACCELERATOR, help="VM accelerator (e.g., hvf, kvm, tcg). Default: auto-detected.")
    parser.add_argument("--cpu-model", default=app_config.CPU_MODEL, help="CPU model to emulate.")
    parser.add_argument("--memory", default=app_config.MEMORY, help="RAM for the VM.")
    parser.add_argument("--smp-cores", type=int, default=app_config.SMP_CORES, help="Number of CPU cores.")
    parser.add_argument("--vga-type", default=None, help="QEMU VGA card type (e.g., std, virtio, qxl). Overrides automatic selection.")

    suppressed_args = {
        "qemu_executable": app_config.QEMU_EXECUTABLE, "seven_zip_executable": app_config.SEVEN_ZIP_EXECUTABLE, "brew_executable": app_config.BREW_EXECUTABLE,
        "uefi_code_path": app_config.UEFI_CODE_PATH, "uefi_vars_path": app_config.UEFI_VARS_PATH,
        "display_type": app_config.DISPLAY_TYPE, "usb_controller": app_config.USB_CONTROLLER, "keyboard_device": app_config.KEYBOARD_DEVICE,
        "mouse_device": app_config.MOUSE_DEVICE, "network_backend": app_config.NETWORK_BACKEND, "network_device": app_config.NETWORK_DEVICE
    }
    for arg, default_val in suppressed_args.items():
        cli_arg = f"--{arg.replace('_', '-')}"
        parser.add_argument(cli_arg, default=default_val, help=argparse.SUPPRESS)

    args = parser.parse_args()
    config = vars(args)

    if args.architecture == 'list':
        print("Available QEMU architectures:\n" + "\n".join(f"  - {arch}" for arch in app_config.SUPPORTED_ARCHITECTURES))
        sys.exit(0)
    if args.architecture not in app_config.SUPPORTED_ARCHITECTURES:
        print(f"Error: Unsupported architecture '{args.architecture}'. Use 'list' for options.", file=sys.stderr)
        sys.exit(1)
    if not args.disk_image:
        parser.error("--disk-image is required.")

    if args.serial_device == 'list':
        default_key = app_config.ARCH_DEFAULT_SERIAL.get(args.architecture, app_config.ARCH_DEFAULT_SERIAL['default'])
        print(f"Available serial device profiles for architecture '{args.architecture}':")
        for key, profile in app_config.SERIAL_DEVICE_PROFILES.items():
            is_default = "(default)" if key == default_key else ""
            print(f"  - {key:<10} {is_default:<10} {profile['description']}")
        sys.exit(0)
    if args.serial_device and args.serial_device not in app_config.SERIAL_DEVICE_PROFILES:
        print(f"Error: Unknown serial device profile '{args.serial_device}'. Use 'list' for options.", file=sys.stderr)
        sys.exit(1)

    is_macos = platform.system() == 'Darwin'
    if is_macos:
        config['qemu_executable'] = f"qemu-system-{config['architecture']}"
    else:
        arch = config['architecture']
        possible_names = [f"qemu-system-{arch}"]

        if arch == 'x86_64':
            possible_names.append("qemu-system-x86")
        elif arch == 'i386':
            possible_names.append("qemu-system-i386")

        for name in possible_names:
            if shutil.which(name):
                config['qemu_executable'] = name
                break
        else:
            print(f"Error: Could not find SYSTEM QEMU executable for {arch}. Tried: {', '.join(possible_names)}", file=sys.stderr)
            print("       Make sure you've installed the system emulator package (qemu-system-x86)", file=sys.stderr)
            sys.exit(1)
    host_arch, guest_arch = platform.machine(), config['architecture']
    is_native = (host_arch == 'arm64' and guest_arch == 'aarch64') or (host_arch == 'x86_64' and guest_arch == 'x86_64')

    if config['accelerator'] is None:
        if sys.platform == "darwin" and is_native:
            config['accelerator'] = 'hvf'
            print("Info: Auto-selected 'hvf' accelerator for native hardware virtualization.")
        else:
            config['accelerator'] = 'tcg'
            print("Info: Auto-selected 'tcg' accelerator for emulation.")

    if not is_macos and config['accelerator'] == 'tcg':
        if config['cpu_model'] == 'host':
            config['cpu_model'] = 'max'
            print("Info: Forced 'max' CPU model for TCG emulation on Linux.")
    elif config['cpu_model'] == 'host' and not is_native:
        config['cpu_model'] = 'max'
        print(f"Info: Auto-selected CPU model '{config['cpu_model']}' for emulation.")

    if not os.path.exists(config["disk_image"]):
        print(f"Error: Disk image not found: {config['disk_image']}", file=sys.stderr)
        sys.exit(1)
    if config["cdrom"] and not os.path.exists(config["cdrom"]):
        print(f"Error: CD-ROM image not found: {config['cdrom']}", file=sys.stderr)
        sys.exit(1)

    if config['architecture'] == 'x86_64' and config['machine_type'] == 'virt':
        config['machine_type'] = 'q35'

    fw_map = {'aarch64': 'edk2-aarch64-code.fd', 'x86_64': 'edk2-x86_64-code.fd', 'riscv64': 'edk2-riscv64-code.fd'}
    if (fw_filename := fw_map.get(config['architecture'])):
        if is_macos:
            qemu_prefix = get_qemu_prefix(config['brew_executable'])
            uefi_code = str(qemu_prefix / "share/qemu" / fw_filename)
        else:
            uefi_code = f"/usr/share/qemu/{fw_filename}"
            if not os.path.exists(uefi_code):
                if config['architecture'] == 'x86_64':
                    uefi_code = "/usr/share/OVMF/OVMF_CODE.fd"

        if not config["uefi_code_path"]:
            config["uefi_code_path"] = uefi_code

        if not config["uefi_vars_path"]:
            disk_path = Path(config["disk_image"])
            config["uefi_vars_path"] = str(disk_path.parent / f"{disk_path.stem}-{config['architecture']}-vars.fd")

        if not os.path.exists(config["uefi_code_path"]):
            print(f"Error: UEFI firmware file not found at: {config['uefi_code_path']}", file=sys.stderr)
            if is_macos:
                print("       Please install via: brew install edk2-qemu", file=sys.stderr)
            else:
                print("       Please install your distribution's UEFI package (e.g., ovmf, edk2)", file=sys.stderr)
            sys.exit(1)

        prepare_uefi_vars_file(config["uefi_vars_path"], config["uefi_code_path"])

    config['uefi_code'] = config.pop('uefi_code_path', None)
    config['uefi_vars'] = config.pop('uefi_vars_path', None)

    build_qemu_args(config)

if __name__ == "__main__":
    main()
