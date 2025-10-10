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
#      ./run-qemu-vm.py --architecture x86_64 --disk-image my-x86.qcow2 --cdrom ubuntu.iso --console text
#      # Script provides integrated terminal with working arrow keys and backspace
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
#   - For BIOS/serial installations, '7z' must be in the PATH (from 'p7zip').
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
import select
import termios
import tty
import socket
import time
import signal

# --- Global Configuration & Executable Paths ---

# List of supported QEMU architectures
SUPPORTED_ARCHITECTURES = [
    "aarch64", "alpha", "arm", "avr", "hppa", "i386", "loongarch64", "m68k",
    "microblaze", "microblazeel", "mips", "mips64", "mips64el", "mipsel",
    "or1k", "ppc", "ppc64", "riscv32", "riscv64", "rx", "s390x", "sh4",
    "sh4eb", "sparc", "sparc64", "tricore", "x86_64", "xtensa", "xtensaeb"
]

# The command for the Homebrew package manager, used to auto-locate QEMU files.
BREW_EXECUTABLE = "brew"
# The specific QEMU binary for emulating a system. This will be set based on --architecture.
QEMU_EXECUTABLE = None
# The command for the 7-Zip archive tool, used to extract kernel/initrd from ISOs.
SEVEN_ZIP_EXECUTABLE = "7z"
# The virtual machine type QEMU will emulate; 'virt' is a modern standard for many architectures.
MACHINE_TYPE = "virt"
# The hardware virtualization framework to use; 'hvf' is native to macOS on ARM.
ACCELERATOR = "hvf"
# The CPU model to emulate; 'host' passes through the host CPU features for best performance.
CPU_MODEL = "host"
# The default amount of RAM to allocate to the virtual machine.
MEMORY = "4G"
# The default number of virtual CPU cores for the guest system.
SMP_CORES = 4
# The path to the UEFI firmware code file; auto-detected if not specified.
UEFI_CODE_PATH = None
# The path to the UEFI variables file, for persistent settings; auto-generated if not specified.
UEFI_VARS_PATH = None
# The virtual graphics device to use in GUI mode; auto-selected if not specified.
GRAPHICS_DEVICE = None
# The QEMU display configuration string for GUI mode.
DISPLAY_TYPE = "default,show-cursor=on"
# The virtual USB controller model.
USB_CONTROLLER = "qemu-xhci"
# The virtual keyboard device to attach in GUI mode.
KEYBOARD_DEVICE = "usb-kbd"
# The virtual mouse/tablet device to attach in GUI mode for accurate cursor tracking.
MOUSE_DEVICE = "usb-tablet"

# --- Network Configuration ---

# The network backend mode for QEMU user-mode networking (SLIRP/NAT).
# Value: "user" enables user-mode networking which provides NAT to the guest.
# This allows the guest to access the internet and the host, but the guest
# is not directly accessible from the host without port forwarding.
NETWORK_MODE = "user"

# The network backend identifier used to link the backend to the network device.
# Value: Any unique string identifier (commonly "net0").
# This ID is referenced by NETWORK_DEVICE to connect the NIC to this backend.
NETWORK_ID = "net0"

# Port forwarding rules for accessing services running inside the guest VM.
# Format: "hostfwd=<protocol>::<host_port>-:<guest_port>[,hostfwd=...]"
#
# Examples:
#   - "hostfwd=tcp::2222-:22" forwards host port 2222 to guest SSH port 22
#   - "hostfwd=tcp::8080-:80" forwards host port 8080 to guest HTTP port 80
#   - Multiple forwards: "hostfwd=tcp::2222-:22,hostfwd=tcp::8080-:80"
#
# To access the guest SSH server from the host:
#   ssh -p 2222 username@localhost
#
# To disable port forwarding, set to empty string: ""
NETWORK_PORT_FORWARDS = "hostfwd=tcp::2222-:22"

# The complete network backend configuration string, assembled from the above variables.
# This is constructed automatically; modify the individual variables above instead.
NETWORK_BACKEND = f"{NETWORK_MODE},id={NETWORK_ID}" + (f",{NETWORK_PORT_FORWARDS}" if NETWORK_PORT_FORWARDS else "")

# The virtual network interface card (NIC) device attached to the guest.
# Format: "<device_model>,netdev=<backend_id>"
# Value: "virtio-net-pci" is the high-performance paravirtualized network device
#        recommended for modern Linux guests. It requires virtio drivers in the guest OS.
# The "netdev=net0" parameter links this NIC to the NETWORK_BACKEND with id "net0".
NETWORK_DEVICE = f"virtio-net-pci,netdev={NETWORK_ID}"

# --- Directory Sharing Configuration ---

# VirtFS security model for shared directories.
# Values:
#   - "mapped-xattr": Stores guest permissions in host extended attributes (recommended)
#   - "mapped-file": Stores permissions in hidden files (fallback if xattr not supported)
#   - "passthrough": Direct mapping (requires QEMU to run as root, not recommended)
#   - "none": No permission mapping (simplest but least secure)
VIRTFS_SECURITY_MODEL = "mapped-xattr"

# VirtFS 9P protocol version.
# Value: "9p2000.L" is the Linux version with better performance and POSIX semantics.
# Alternative: "9p2000.u" for older systems.
VIRTFS_VERSION = "9p2000.L"

# Valid character pattern for VirtFS mount tags.
# Mount tags are identifiers used by QEMU and the 9P protocol to reference
# shared filesystems. They must be simple identifiers without special characters
# that could be misinterpreted by shells, filesystems, or QEMU itself.
#
# Pattern breakdown: ^[a-zA-Z0-9_]+$
#   ^        - Start of string (no leading characters allowed)
#   [...]    - Character class (only these characters allowed)
#   a-z      - Lowercase letters
#   A-Z      - Uppercase letters
#   0-9      - Digits
#   _        - Underscore ONLY (hyphens are NOT allowed)
#   +        - One or more characters (empty tags not allowed)
#   $        - End of string (no trailing characters allowed)
#
# Why these restrictions?
#   1. Mount tags are used as identifiers in QEMU command-line arguments
#   2. They become filesystem mount points in the 9P protocol
#   3. They are used as device IDs in QEMU's internal device tree
#   4. Hyphens could be confused with command-line flags (--mount-tag)
#   5. Spaces would break shell argument parsing
#   6. Special characters might have meaning in filesystem contexts
#   7. Unicode characters could cause encoding issues
#
# Examples of VALID mount tags:
#   hostshare, myfiles, shared_data, project123, MY_DOCS
#
# Examples of INVALID mount tags:
#   shared-dir (contains hyphen)
#   my files (contains space)
#   data! (contains exclamation mark)
#   café (contains non-ASCII character)
MOUNT_TAG_PATTERN = r'^[a-zA-Z0-9_]+$'

# Human-readable description of allowed characters for error messages.
MOUNT_TAG_ALLOWED_CHARS = "letters (a-z, A-Z), numbers (0-9), and underscores (_)"

# --- Text Console Mode Constants ---
MODE_SERIAL_CONSOLE = 'serial_console'
MODE_CONTROL_MENU = 'control_menu'
MODE_QEMU_MONITOR = 'qemu_monitor'

def get_qemu_prefix(brew_executable):
    """Finds the Homebrew installation prefix for QEMU."""
    try:
        prefix = subprocess.check_output(
            [brew_executable, "--prefix", "qemu"],
            text=True,
            stderr=subprocess.PIPE
        ).strip()
        return Path(prefix)
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        print(
            f"Error: Could not find QEMU prefix using '{brew_executable}'. "
            f"Is Homebrew installed and in your PATH? Error: {e}",
            file=sys.stderr
        )
        sys.exit(1)

def detect_firmware_type(iso_path, architecture):
    """
    Detects the firmware type for an ISO based on architecture and filename.
    Defaults to 'uefi' for common modern architectures.
    """
    if not iso_path:
        # Default to UEFI for common 64-bit architectures if no ISO is provided
        if architecture in ['aarch64', 'x86_64', 'riscv64']:
            return 'uefi'
        return 'bios' # Fallback for other architectures

    # Specific filename check
    if 'bios' in Path(iso_path).name.lower():
         print("Info: ISO filename suggests BIOS/direct-kernel boot.")
         return 'bios'

    # For AArch64, UEFI is standard
    if architecture == 'aarch64':
        return 'uefi'

    # Default to UEFI for other common 64-bit architectures
    if architecture in ['x86_64', 'riscv64']:
        return 'uefi'

    # Fallback default
    return 'uefi'


def find_uefi_bootloader(seven_zip_executable, iso_path, architecture):
    """Inspects an ISO to find the UEFI bootloader file for the given architecture."""
    bootloader_patterns = {
        'aarch64': ['bootaa64.efi'],
        'x86_64': ['bootx64.efi'],
        'riscv64': ['bootriscv64.efi']
        # Add other architecture bootloader filenames here if needed
    }
    patterns = bootloader_patterns.get(architecture)
    if not patterns:
        print(f"Warning: No known UEFI bootloader pattern for architecture '{architecture}'.", file=sys.stderr)
        return None, None

    print(f"Info: Searching for UEFI bootloader in '{iso_path}' for {architecture}...")
    try:
        result = subprocess.run(
            [seven_zip_executable, 'l', iso_path],
            capture_output=True, text=True, check=True
        )
    except (FileNotFoundError, subprocess.CalledProcessError) as e:
        print(f"Warning: Could not list files in ISO with '{seven_zip_executable}'. Cannot automate boot.", file=sys.stderr)
        if hasattr(e, 'stderr'):
            print(e.stderr, file=sys.stderr)
        return None, None

    bootloader_full_path = None
    for line in result.stdout.splitlines():
        for pattern in patterns:
            if pattern in line.lower():
                parts = line.split()
                if len(parts) > 0 and parts[-1].lower().endswith(pattern):
                    if 'efi' in parts[-1].lower() and 'boot' in parts[-1].lower():
                        bootloader_full_path = parts[-1]
                        break
        if bootloader_full_path:
            break

    if bootloader_full_path:
        p = Path(bootloader_full_path)
        bootloader_name = p.name
        bootloader_script_path = str(p).replace('/', '\\')
        print(f"Info: Found UEFI bootloader: {bootloader_name} at path {bootloader_full_path}")
        return bootloader_name, bootloader_script_path

    print(f"Warning: Could not find a suitable bootloader ({', '.join(patterns)}) in the ISO. Automatic boot may fail.", file=sys.stderr)
    return None, None


def parse_share_dir_argument(share_dir_arg):
    """
    Parse the --share-dir argument into host path and mount tag.

    Format: /host/path:mount_tag

    Performs comprehensive validation:
      - Checks format has exactly one colon separator
      - Validates host path exists and is a directory
      - Validates mount tag contains only allowed characters
      - Provides detailed, actionable error messages with visual indicators

    Returns: (host_path, mount_tag) or (None, None) if invalid
    """
    if not share_dir_arg:
        return None, None

    # Check for colon separator
    if ':' not in share_dir_arg:
        print(f"Error: --share-dir format must be '/host/path:mount_tag'", file=sys.stderr)
        print(f"       Missing colon (:) separator", file=sys.stderr)
        print(f"", file=sys.stderr)
        print(f"       You provided: {share_dir_arg}", file=sys.stderr)
        print(f"       Expected format: /path/to/directory:mount_tag", file=sys.stderr)
        print(f"", file=sys.stderr)
        print(f"       Examples:", file=sys.stderr)
        print(f"         --share-dir /Users/myuser/projects:myprojects", file=sys.stderr)
        print(f"         --share-dir ~/Documents:docs", file=sys.stderr)
        print(f"         --share-dir /tmp/shared:shared_data", file=sys.stderr)
        return None, None

    # Split on last colon to handle paths with colons (rare but possible)
    parts = share_dir_arg.rsplit(':', 1)
    if len(parts) != 2:
        print(f"Error: Invalid --share-dir format: {share_dir_arg}", file=sys.stderr)
        return None, None

    host_path, mount_tag = parts

    # Validate host path exists
    if not os.path.exists(host_path):
        print(f"Error: Host directory does not exist: {host_path}", file=sys.stderr)
        print(f"", file=sys.stderr)
        print(f"       Please create the directory first:", file=sys.stderr)
        print(f"         mkdir -p {host_path}", file=sys.stderr)
        return None, None

    if not os.path.isdir(host_path):
        print(f"Error: Host path is not a directory: {host_path}", file=sys.stderr)
        print(f"", file=sys.stderr)
        print(f"       The path exists but is a file, not a directory.", file=sys.stderr)
        print(f"       Please specify a directory to share.", file=sys.stderr)
        return None, None

    # Validate mount tag - find ALL invalid characters and their positions
    invalid_chars = []
    for i, char in enumerate(mount_tag):
        if not re.match(r'[a-zA-Z0-9_]', char):
            invalid_chars.append((i, char))

    if invalid_chars:
        # Build detailed error message with visual indicators
        print(f"Error: Invalid characters in mount tag '{mount_tag}'", file=sys.stderr)
        print(f"       {mount_tag}", file=sys.stderr)

        # Create a line with carets pointing to invalid characters
        caret_line = [' '] * len(mount_tag)
        for pos, char in invalid_chars:
            caret_line[pos] = '^'
        print(f"       {''.join(caret_line)}", file=sys.stderr)

        # List each invalid character with its position and description
        for pos, char in invalid_chars:
            char_name = {
                ' ': 'space',
                '-': 'hyphen',
                '.': 'period',
                '/': 'forward slash',
                '\\': 'backslash',
                '!': 'exclamation mark',
                '@': 'at sign',
                '#': 'hash',
                '$': 'dollar sign',
                '%': 'percent',
                '^': 'caret',
                '&': 'ampersand',
                '*': 'asterisk',
                '(': 'left parenthesis',
                ')': 'right parenthesis',
                '+': 'plus sign',
                '=': 'equals sign',
                '[': 'left bracket',
                ']': 'right bracket',
                '{': 'left brace',
                '}': 'right brace',
                '|': 'pipe',
                ';': 'semicolon',
                ':': 'colon',
                "'": 'single quote',
                '"': 'double quote',
                '<': 'less than',
                '>': 'greater than',
                ',': 'comma',
                '?': 'question mark',
                '~': 'tilde',
                '`': 'backtick'
            }.get(char, 'special character')

            print(f"       Position {pos + 1}: '{char}' ({char_name}) is not allowed", file=sys.stderr)

        print(f"", file=sys.stderr)
        print(f"       Mount tags may only contain:", file=sys.stderr)
        print(f"         • Letters: a-z, A-Z", file=sys.stderr)
        print(f"         • Numbers: 0-9", file=sys.stderr)
        print(f"         • Underscores: _", file=sys.stderr)
        print(f"", file=sys.stderr)
        print(f"       Hyphens (-), spaces, and special characters are NOT allowed.", file=sys.stderr)
        print(f"", file=sys.stderr)

        # Suggest a corrected version
        suggested = mount_tag
        # Replace common problematic characters with underscores
        for char in ['-', ' ', '.', '/', '\\']:
            suggested = suggested.replace(char, '_')
        # Remove any remaining invalid characters
        suggested = re.sub(r'[^a-zA-Z0-9_]', '', suggested)
        # Collapse multiple underscores
        suggested = re.sub(r'_+', '_', suggested)
        # Remove leading/trailing underscores
        suggested = suggested.strip('_')

        if suggested and suggested != mount_tag:
            print(f"       Suggested fix: {suggested}", file=sys.stderr)
            print(f"       Use: --share-dir {host_path}:{suggested}", file=sys.stderr)
        else:
            print(f"       Valid examples: hostshare, myfiles, shared_data", file=sys.stderr)

        return None, None

    # Validate mount tag is not empty (edge case after split)
    if not mount_tag:
        print(f"Error: Mount tag cannot be empty", file=sys.stderr)
        print(f"       Format: /host/path:mount_tag", file=sys.stderr)
        return None, None

    # Convert to absolute path
    host_path = os.path.abspath(host_path)

    return host_path, mount_tag


def create_and_run_uefi_with_automation(base_args, config):
    """
    Creates a temporary startup.nsh to automate UEFI boot and runs QEMU.
    """
    bootloader_name, bootloader_script_path = find_uefi_bootloader(
        config['seven_zip_executable'], config['cdrom'], config['architecture']
    )

    if bootloader_name and bootloader_script_path:
        with tempfile.TemporaryDirectory(prefix="qemu-uefi-boot-") as temp_dir:
            startup_script_path = Path(temp_dir) / "startup.nsh"

            script_content = (
                "# Auto-generated by run-qemu-vm.py to automate UEFI boot\n"
                "echo -off\n"
                "echo 'Attempting to boot from CD-ROM...'\n"
                "FS0:\n"
                f"{bootloader_script_path}\n"
            )

            with open(startup_script_path, "w") as f:
                f.write(script_content)

            print(f"Info: Created temporary startup.nsh in '{temp_dir}'")

            automated_args = list(base_args)
            automated_args.extend([
                "-drive", f"if=none,id=boot-script,format=raw,file=fat:rw:{temp_dir}",
                "-device", "usb-storage,drive=boot-script"
            ])

            run_qemu(automated_args, config)
    else:
        print("Warning: Proceeding without boot automation script.", file=sys.stderr)
        run_qemu(base_args, config)


class TextConsoleManager:
    """Manages the text console mode with key translation and mode switching."""

    def __init__(self, qemu_process, pty_device, monitor_socket):
        self.qemu_process = qemu_process
        self.pty_device = pty_device
        self.monitor_socket_path = monitor_socket
        self.pty_fd = None
        self.monitor_sock = None
        self.original_settings = None
        self.current_mode = MODE_SERIAL_CONSOLE
        self.stdin_fd = sys.stdin.fileno()
        self.stdout_fd = sys.stdout.fileno()

    def setup(self):
        """Initialize PTY and monitor connections."""
        try:
            # Open PTY device
            self.pty_fd = os.open(self.pty_device, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)

            # Set PTY to raw mode
            try:
                attrs = termios.tcgetattr(self.pty_fd)
                attrs[0] = 0  # iflag
                attrs[1] = 0  # oflag
                attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL  # cflag
                attrs[3] = 0  # lflag
                termios.tcsetattr(self.pty_fd, termios.TCSANOW, attrs)
            except:
                pass

            # Connect to monitor socket
            self.monitor_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)

            # Wait for monitor socket to be ready
            for _ in range(20):
                try:
                    self.monitor_sock.connect(self.monitor_socket_path)
                    break
                except (FileNotFoundError, ConnectionRefusedError):
                    time.sleep(0.1)
            else:
                print("Warning: Could not connect to QEMU monitor", file=sys.stderr)

            # Consume initial monitor output
            self.monitor_sock.setblocking(False)
            try:
                while True:
                    data = self.monitor_sock.recv(4096)
                    if not data:
                        break
            except BlockingIOError:
                pass

            # Save terminal settings and enter raw mode
            self.original_settings = termios.tcgetattr(self.stdin_fd)
            tty.setraw(self.stdin_fd)

            print(f"\nConnected to serial console: {self.pty_device}")
            print("Press Ctrl-] for control menu.\n")
            sys.stdout.flush()

            return True

        except Exception as e:
            print(f"Error setting up text console: {e}", file=sys.stderr)
            import traceback
            traceback.print_exc()
            return False

    def translate_keys(self, data):
        """Translate key sequences for GRUB compatibility."""
        # Translate DEL (0x7f) to BS (0x08) for backspace
        return data.replace(b'\x7f', b'\x08')

    def enter_raw_mode(self):
        """Put terminal in raw mode."""
        tty.setraw(self.stdin_fd)

    def restore_terminal(self):
        """Restore terminal to original settings."""
        if self.original_settings:
            termios.tcsetattr(self.stdin_fd, termios.TCSADRAIN, self.original_settings)

    def show_control_menu(self):
        """Display control menu and handle user choice."""
        self.restore_terminal()

        print("\n")
        print("╔════════════════════════════════════╗")
        print("║ run-qemu-vm.py Control Menu        ║")
        print("╠════════════════════════════════════╣")
        print("║ q - Quit QEMU and exit             ║")
        print("║ m - Enter QEMU monitor             ║")
        print("║ r - Resume serial console          ║")
        print("╚════════════════════════════════════╝")
        print()
        print("Choice: ", end='', flush=True)

        choice = sys.stdin.read(1)

        if choice.lower() == 'q':
            self.quit_qemu()
            return False  # Signal to exit main loop
        elif choice.lower() == 'm':
            self.current_mode = MODE_QEMU_MONITOR
            print("\nEntering QEMU monitor (Ctrl-] for menu)...")
            print("(qemu) ", end='', flush=True)
            self.enter_raw_mode()
        else:  # 'r' or anything else
            print("\nResuming serial console...")
            sys.stdout.flush()
            self.current_mode = MODE_SERIAL_CONSOLE
            self.enter_raw_mode()

        return True  # Continue main loop

    def quit_qemu(self):
        """Gracefully shutdown QEMU."""
        print("\nShutting down VM...")
        sys.stdout.flush()

        try:
            # Send quit command to monitor
            self.monitor_sock.send(b"quit\n")

            # Wait up to 5 seconds for graceful shutdown
            for _ in range(50):
                if self.qemu_process.poll() is not None:
                    break
                time.sleep(0.1)
            else:
                # Force kill if still running
                self.qemu_process.kill()
                self.qemu_process.wait()
        except:
            # If monitor command fails, just kill the process
            self.qemu_process.kill()
            self.qemu_process.wait()

        print("VM stopped.")

    def cleanup(self):
        """Clean up resources."""
        self.restore_terminal()

        if self.pty_fd:
            try:
                os.close(self.pty_fd)
            except:
                pass

        if self.monitor_sock:
            try:
                self.monitor_sock.close()
            except:
                pass

    def run_serial_console_mode(self):
        """Handle serial console interaction."""
        try:
            readable, _, _ = select.select([self.stdin_fd, self.pty_fd], [], [], 0.1)

            if self.stdin_fd in readable:
                # User typed something
                try:
                    data = os.read(self.stdin_fd, 1024)

                    if b'\x1d' in data:  # Ctrl-]
                        return self.show_control_menu()

                    # Translate keys and send to guest
                    translated = self.translate_keys(data)
                    os.write(self.pty_fd, translated)

                except OSError:
                    pass

            if self.pty_fd in readable:
                # Guest sent output
                try:
                    data = os.read(self.pty_fd, 4096)
                    if data:
                        os.write(self.stdout_fd, data)
                except OSError:
                    pass

            return True

        except Exception as e:
            print(f"\nError in serial console mode: {e}", file=sys.stderr)
            return False

    def run_monitor_mode(self):
        """Handle QEMU monitor interaction."""
        try:
            readable, _, _ = select.select([self.stdin_fd, self.monitor_sock], [], [], 0.1)

            if self.stdin_fd in readable:
                # User typed something
                try:
                    data = os.read(self.stdin_fd, 1024)

                    if b'\x1d' in data:  # Ctrl-]
                        return self.show_control_menu()

                    # Send to monitor (no translation)
                    self.monitor_sock.send(data)

                except OSError:
                    pass

            if self.monitor_sock in readable:
                # Monitor sent output
                try:
                    data = self.monitor_sock.recv(4096)
                    if data:
                        os.write(self.stdout_fd, data)
                except OSError:
                    pass

            return True

        except Exception as e:
            print(f"\nError in monitor mode: {e}", file=sys.stderr)
            return False

    def run(self):
        """Main event loop."""
        try:
            while True:
                # Check if QEMU died
                if self.qemu_process.poll() is not None:
                    self.restore_terminal()
                    print("\n\nQEMU exited.")
                    return self.qemu_process.returncode

                # Route to appropriate mode handler
                if self.current_mode == MODE_SERIAL_CONSOLE:
                    if not self.run_serial_console_mode():
                        return 0
                elif self.current_mode == MODE_QEMU_MONITOR:
                    if not self.run_monitor_mode():
                        return 0

        except KeyboardInterrupt:
            self.restore_terminal()
            print("\n\nInterrupted. Shutting down...")
            self.quit_qemu()
            return 130
        finally:
            self.cleanup()


def parse_pty_device(qemu_process):
    """Parse QEMU output to find the PTY device path."""
    import fcntl

    # Set stdout to non-blocking
    flags = fcntl.fcntl(qemu_process.stdout, fcntl.F_GETFL)
    fcntl.fcntl(qemu_process.stdout, fcntl.F_SETFL, flags | os.O_NONBLOCK)

    pty_device = None
    start_time = time.time()

    while time.time() - start_time < 5:  # Wait up to 5 seconds
        try:
            line = qemu_process.stdout.readline()
            if line:
                # Look for: char device redirected to /dev/ttysXXX (label char0)
                match = re.search(r'char device redirected to (/dev/[^\s]+)', line)
                if match:
                    pty_device = match.group(1)
                    print(f"Serial console: {pty_device}")
                    break
        except IOError:
            pass

        time.sleep(0.1)

    return pty_device


def build_qemu_args(config):
    """Constructs the list of arguments for the QEMU command from the config."""
    base_args = [
        config["qemu_executable"],
        "-M", config["machine_type"],
        "-accel", config["accelerator"],
        "-cpu", config["cpu_model"],
        "-m", config["memory"],
        "-smp", str(config["smp_cores"]),
        "-netdev", config["network_backend"],
        "-device", config["network_device"],
        "-hda", config["disk_image"],
        "-device", config["usb_controller"],
    ]

    if config["cdrom"]:
        base_args.extend(["-cdrom", config["cdrom"]])

    # --- Directory Sharing via VirtFS (Optional) ---
    if config.get("share_dir"):
        host_path, mount_tag = parse_share_dir_argument(config["share_dir"])
        if host_path and mount_tag:
            print(f"Info: Sharing host directory '{host_path}' as '{mount_tag}'")
            print(f"      Mount in guest with: sudo mkdir -p /mnt/{mount_tag} && sudo mount -t 9p -o trans=virtio,version={VIRTFS_VERSION} {mount_tag} /mnt/{mount_tag}")

            # Build VirtFS arguments
            virtfs_args = [
                "-virtfs",
                f"local,path={host_path},mount_tag={mount_tag},security_model={VIRTFS_SECURITY_MODEL},id={mount_tag}"
            ]
            base_args.extend(virtfs_args)
        else:
            print("Error: Invalid --share-dir argument. Directory sharing disabled.", file=sys.stderr)
            sys.exit(1)

    # --- Firmware-Specific Configuration ---
    if config['firmware'] == 'bios':
        with tempfile.TemporaryDirectory(prefix="qemu-bios-boot-") as temp_dir:
            print(f"Info: Extracting kernel and initrd to temporary directory: {temp_dir}")
            try:
                # This part is highly specific to the ISO structure and may fail.
                # Example for Debian-like aarch64 installers
                kernel_file = "install.a64/vmlinuz"
                initrd_file = "install.a64/initrd.gz"

                subprocess.run(
                    [
                        config["seven_zip_executable"], "e", config["cdrom"],
                        kernel_file, initrd_file,
                        f"-o{temp_dir}"
                    ],
                    check=True, capture_output=True, text=True
                )
            except (FileNotFoundError, subprocess.CalledProcessError) as e:
                print(f"Error extracting kernel/initrd: {e.stderr}", file=sys.stderr)
                sys.exit(1)

            kernel_path = os.path.join(temp_dir, "vmlinuz")
            initrd_path = os.path.join(temp_dir, "initrd.gz")
            if not (os.path.exists(kernel_path) and os.path.exists(initrd_path)):
                print("Error: Could not find vmlinuz or initrd.gz in the ISO.", file=sys.stderr)
                sys.exit(1)

            final_args = list(base_args)
            final_args.extend(["-kernel", kernel_path, "-initrd", initrd_path])

            if config['console'] == 'text':
                print("Info: Using text-only (serial) console for BIOS boot.")
                final_args.extend(["-nographic", "-append", "console=ttyAMA0"])
            else:
                print("Info: Using graphical (GUI) console for BIOS boot.")

            run_qemu(final_args, config)
            return

    # --- UEFI Configuration (Default) ---
    if config.get('uefi_code') and config.get('uefi_vars'):
        uefi_args = [
            "-drive", f"if=pflash,format=raw,readonly=on,file={config['uefi_code']}",
            "-drive", f"if=pflash,format=raw,file={config['uefi_vars']}",
        ]
        final_args = list(base_args)
        final_args.extend(uefi_args)
    else:
        print("Warning: UEFI firmware paths not set. Proceeding without UEFI.", file=sys.stderr)
        final_args = list(base_args)


    if config['console'] == 'text':
        print("Info: Using text-only (serial) console with integrated terminal.")

        # Create monitor socket path
        monitor_socket = f"/tmp/qemu-monitor-{os.getpid()}.sock"
        config['monitor_socket'] = monitor_socket

        final_args.extend([
            "-monitor", f"unix:{monitor_socket},server,nowait",
            "-chardev", "pty,id=char0",
            "-serial", "chardev:char0",
            "-nographic",
        ])
    else:  # gui
        print("Info: Using graphical (GUI) console.")
        if not config['graphics_device']:
            # Select a default graphics device based on architecture
            if config['architecture'] in ['x86_64', 'aarch64']:
                 config['graphics_device'] = 'virtio-gpu-pci'
            else:
                 config['graphics_device'] = 'VGA' # A safe fallback
        final_args.extend([
            "-device", config["graphics_device"],
            "-display", config["display_type"],
            "-device", config["keyboard_device"],
            "-device", config["mouse_device"],
        ])

    boot_device = config.get("boot_from") or ('cdrom' if config["cdrom"] else 'hd')
    boot_order = 'd' if boot_device == 'cdrom' else 'c'
    final_args.extend(["-boot", f"order={boot_order}"])

    if boot_device == 'cdrom' and config["cdrom"] and config['firmware'] == 'uefi':
        create_and_run_uefi_with_automation(final_args, config)
    else:
        run_qemu(final_args, config)


def prepare_uefi_vars_file(vars_path, code_path):
    """
    Ensures the UEFI variables file is valid.
    """
    should_create = False
    try:
        code_size = os.path.getsize(code_path)
    except FileNotFoundError:
        print(f"Error: UEFI code file not found at '{code_path}'", file=sys.stderr)
        sys.exit(1)

    if not os.path.exists(vars_path):
        print(f"UEFI variables file not found. Creating a new one at: {vars_path}")
        should_create = True
    else:
        vars_size = os.path.getsize(vars_path)
        if vars_size != code_size:
            print(
                f"Warning: UEFI variables file at '{vars_path}' has incorrect size "
                f"({vars_size} bytes, expected {code_size} bytes). Recreating it."
            )
            should_create = True

    if should_create:
        try:
            shutil.copyfile(code_path, vars_path)
        except IOError as e:
            print(f"Error: Could not create UEFI variables file: {e}", file=sys.stderr)
            sys.exit(1)


def run_qemu(args, config):
    """Executes the QEMU command and handles text console if needed."""
    print("--- Starting QEMU with the following command ---")
    print(subprocess.list2cmdline(args))
    print("-------------------------------------------------")

    try:
        if config.get('console') == 'text':
            # Start QEMU with stdout capture to parse PTY device
            process = subprocess.Popen(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1
            )

            # Parse PTY device from output
            pty_device = parse_pty_device(process)

            if not pty_device:
                print("Error: Could not find PTY device in QEMU output", file=sys.stderr)
                process.kill()
                sys.exit(1)

            # Create and run text console manager
            console = TextConsoleManager(process, pty_device, config['monitor_socket'])

            if not console.setup():
                process.kill()
                sys.exit(1)

            return_code = console.run()
            sys.exit(return_code)

        else:
            # GUI mode - just run normally
            process = subprocess.Popen(args)
            process.wait()

    except FileNotFoundError:
        print(f"Error: QEMU executable '{args[0]}' not found", file=sys.stderr)
        print(f"       Please ensure QEMU is installed and '{args[0]}' is in your PATH.", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nInterrupted")
        sys.exit(130)

    if 'process' in locals() and process.returncode != 0:
        print(f"\nQEMU exited with an error (code {process.returncode}).", file=sys.stderr)
        sys.exit(process.returncode)

def main():
    """Parses command-line arguments and launches the VM."""
    parser = argparse.ArgumentParser(
        description="Launch a QEMU virtual machine with a specified architecture, display, and firmware options.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Directory Sharing (VirtFS):
  Use --share-dir to share a host directory with the guest via VirtFS (9P).

  Format: /host/path:mount_tag

  Mount tag restrictions (IMPORTANT):
    • Must contain ONLY: letters (a-z, A-Z), numbers (0-9), underscores (_)
    • Hyphens (-), spaces, and special characters are NOT allowed
    • Cannot be empty

  Examples:
    --share-dir /Users/myuser/projects:myprojects
    --share-dir ~/Documents:docs
    --share-dir /tmp/data:shared_data

  Inside the guest, mount with:
    sudo mkdir -p /mnt/myprojects
    sudo mount -t 9p -o trans=virtio,version=9p2000.L myprojects /mnt/myprojects

  To make it persistent, add to /etc/fstab in the guest:
    myprojects  /mnt/myprojects  9p  trans=virtio,version=9p2000.L,_netdev  0  0

  Note: Guest kernel must have 9P filesystem support (CONFIG_9P_FS).
        """
    )

    # --- Argument Definitions ---
    parser.add_argument(
        "--architecture",
        required=True,
        help="The QEMU system architecture to use (e.g., aarch64, x86_64, riscv64). "
             "Use 'list' to see all available architectures."
    )
    parser.add_argument("--disk-image", help="Path to the primary virtual hard disk image (.qcow2).")
    parser.add_argument("--cdrom", help="Path to a bootable ISO file (for installation).")
    parser.add_argument(
        "--boot-from", choices=['cdrom', 'hd'],
        help="Specify the boot device for UEFI mode. If --cdrom is used, the default is 'cdrom'. Otherwise, the default is 'hd'."
    )
    parser.add_argument(
        "--firmware", choices=['uefi', 'bios'],
        help="Specify firmware mode. 'uefi' (default for many archs) uses firmware to boot. 'bios' uses direct kernel boot."
    )
    parser.add_argument(
        "--console", choices=['gui', 'text'], default='gui',
        help="Choose the console type. 'gui' for a graphical window, 'text' for an integrated serial console with key translation."
    )
    parser.add_argument(
        "--share-dir",
        metavar="/HOST/PATH:MOUNT_TAG",
        help="Share a host directory with the guest via VirtFS. "
             "Format: /host/path:mount_tag (e.g., /Users/me/data:mydata). "
             "Mount tag must contain only letters, numbers, and underscores (NO hyphens or spaces). "
             "Mount in guest: sudo mount -t 9p -o trans=virtio mount_tag /mnt/mountpoint"
    )

    # Executable paths
    parser.add_argument("--qemu-executable", default=QEMU_EXECUTABLE, help=argparse.SUPPRESS) # Suppress from help
    parser.add_argument("--seven-zip-executable", default=SEVEN_ZIP_EXECUTABLE, help="Path to the 7z binary.")
    parser.add_argument("--brew-executable", default=BREW_EXECUTABLE, help="Path to the Homebrew binary.")

    # VM configuration
    parser.add_argument("--machine-type", default=MACHINE_TYPE, help="QEMU machine type (e.g., virt, q35).")
    parser.add_argument("--accelerator", default=ACCELERATOR, help="VM accelerator to use (e.g., hvf, kvm, tcg).")
    parser.add_argument("--cpu-model", default=CPU_MODEL, help="CPU model to emulate.")
    parser.add_argument("--memory", default=MEMORY, help="RAM to allocate to the VM.")
    parser.add_argument("--smp-cores", type=int, default=SMP_CORES, help="Number of CPU cores for the VM.")
    parser.add_argument("--uefi-code", default=UEFI_CODE_PATH, help="Path to UEFI firmware code. (Default: auto-detected)")
    parser.add_argument("--uefi-vars", default=UEFI_VARS_PATH, help="Path to UEFI variables file. Defaults to a descriptive name next to the disk image.")
    parser.add_argument("--graphics-device", default=None, help="Virtual graphics device. Auto-selected for GUI mode if not specified.")
    parser.add_argument("--display-type", default=DISPLAY_TYPE, help="QEMU display configuration.")
    parser.add_argument("--usb-controller", default=USB_CONTROLLER, help="Virtual USB controller.")
    parser.add_argument("--keyboard-device", default=KEYBOARD_DEVICE, help="Virtual keyboard device.")
    parser.add_argument("--mouse-device", default=MOUSE_DEVICE, help="Virtual mouse/tablet device.")
    parser.add_argument("--network-backend", default=NETWORK_BACKEND, help="Network backend configuration.")
    parser.add_argument("--network-device", default=NETWORK_DEVICE, help="Virtual network interface.")

    args = parser.parse_args()

    # Handle --architecture list
    if args.architecture == 'list':
        print("Available QEMU architectures:")
        for arch in SUPPORTED_ARCHITECTURES:
            print(f"  - {arch}")
        sys.exit(0)

    # Validate architecture
    if args.architecture not in SUPPORTED_ARCHITECTURES:
        print(f"Error: Unsupported architecture '{args.architecture}'.", file=sys.stderr)
        print("Use '--architecture list' to see available options.", file=sys.stderr)
        sys.exit(1)

    # A disk image is required unless listing architectures
    if not args.disk_image:
        parser.error("--disk-image is required when running a VM.")

    config = vars(args)

    # --- Post-processing and Validation ---
    config['qemu_executable'] = f"qemu-system-{config['architecture']}"

    if not os.path.exists(config["disk_image"]):
        print(f"Error: Disk image not found: {config['disk_image']}", file=sys.stderr)
        sys.exit(1)
    if config["cdrom"] and not os.path.exists(config["cdrom"]):
        print(f"Error: CD-ROM image not found: {config['cdrom']}", file=sys.stderr)
        sys.exit(1)

    config['firmware'] = config.get('firmware') or detect_firmware_type(config.get('cdrom'), config['architecture'])

    # Architecture-specific defaults
    if config['architecture'] == 'x86_64':
        if config['machine_type'] == 'virt': # virt is not ideal for x86
            config['machine_type'] = 'q35' # q35 is a more modern default for x86_64

    if config['firmware'] == 'uefi':
        uefi_fw_files = {
            'aarch64': 'edk2-aarch64-code.fd',
            'x86_64': 'edk2-x86_64-code.fd',
            'riscv64': 'edk2-riscv64-code.fd'
            # Add other firmware files here
        }
        fw_filename = uefi_fw_files.get(config['architecture'])

        if fw_filename:
            qemu_prefix = get_qemu_prefix(config['brew_executable'])
            if not config["uefi_code"]:
                config["uefi_code"] = str(qemu_prefix / "share/qemu" / fw_filename)
            if not config["uefi_vars"]:
                disk_path = Path(config["disk_image"])
                disk_stem = disk_path.stem
                vars_filename = f"{disk_stem}-{config['architecture']}-vars.fd"
                config["uefi_vars"] = str(disk_path.parent / vars_filename)

            if os.path.exists(config["uefi_code"]):
                prepare_uefi_vars_file(config["uefi_vars"], config["uefi_code"])
            else:
                print(f"Warning: UEFI firmware '{fw_filename}' not found at expected path. Disabling UEFI.", file=sys.stderr)
                config['firmware'] = 'bios' # Fallback to BIOS if firmware not found
        else:
            print(f"Info: No standard UEFI firmware file known for architecture '{config['architecture']}'. Assuming BIOS boot.", file=sys.stderr)
            config['firmware'] = 'bios'


    build_qemu_args(config)

if __name__ == "__main__":
    main()
