#!/usr/bin/env python3

#
# Description:
#   This script launches a QEMU virtual machine to install or run an AArch64
#   (ARM64) operating system. It intelligently selects the required firmware
#   (UEFI or BIOS), and allows the user to choose between a graphical (GUI)
#   or text-based (serial) console, with all combinations being supported.
#
#   In text console mode, the script provides built-in key translation for
#   GRUB compatibility (backspace, arrow keys) and an integrated control menu
#   for accessing the QEMU monitor.
#
# Usage:
#   1. UEFI installation with a GUI:
#      ./run-qemu-vm.py --disk-image my-os.qcow2 --cdrom uefi-installer.iso --console gui
#
#   2. UEFI installation with a text-only terminal:
#      ./run-qemu-vm.py --disk-image my-os.qcow2 --cdrom uefi-installer.iso --console text
#      # Script provides integrated terminal with working arrow keys and backspace
#      # Press Ctrl-] for control menu
#
#   3. Share a directory with the guest:
#      ./run-qemu-vm.py --disk-image my-os.qcow2 --share-dir /path/on/host:sharename
#      # Inside guest: sudo mount -t 9p -o trans=virtio sharename /mnt/shared
#
#   4. To see all available options:
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

# The command for the Homebrew package manager, used to auto-locate QEMU files.
BREW_EXECUTABLE = "brew"
# The specific QEMU binary for emulating a 64-bit ARM system.
QEMU_EXECUTABLE = "qemu-system-aarch64"
# The command for the 7-Zip archive tool, used to extract kernel/initrd from ISOs.
SEVEN_ZIP_EXECUTABLE = "7z"
# The virtual machine type QEMU will emulate; 'virt' is the modern standard for ARM.
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

def detect_firmware_type(iso_path):
    """
    Detects the firmware type for an AArch64 ISO.
    For AArch64, the standard is UEFI. This function defaults to 'uefi'
    and only switches to 'bios' for special-purpose direct-kernel images.
    """
    if not iso_path:
        return 'uefi'

    if 'bios' in Path(iso_path).name.lower():
         print("Info: ISO filename suggests BIOS/direct-kernel boot.")
         return 'bios'

    return 'uefi'

def find_uefi_bootloader(seven_zip_executable, iso_path):
    """Inspects an ISO to find the AArch64 UEFI bootloader file."""
    print(f"Info: Searching for UEFI bootloader in '{iso_path}'...")
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
        if "bootaa64.efi" in line.lower():
            parts = line.split()
            if len(parts) > 0 and parts[-1].lower().endswith('bootaa64.efi'):
                 if 'efi' in parts[-1].lower() and 'boot' in parts[-1].lower():
                    bootloader_full_path = parts[-1]
                    break

    if bootloader_full_path:
        p = Path(bootloader_full_path)
        bootloader_name = p.name
        bootloader_script_path = str(p).replace('/', '\\')
        print(f"Info: Found UEFI bootloader: {bootloader_name} at path {bootloader_full_path}")
        return bootloader_name, bootloader_script_path

    print("Warning: Could not find 'bootaa64.efi' in the ISO. Automatic boot may fail.", file=sys.stderr)
    return None, None


def parse_share_dir_argument(share_dir_arg):
    """
    Parse the --share-dir argument into host path and mount tag.

    Format: /host/path:mount_tag

    Returns: (host_path, mount_tag) or (None, None) if invalid
    """
    if not share_dir_arg:
        return None, None

    if ':' not in share_dir_arg:
        print(f"Error: --share-dir format must be '/host/path:mount_tag'", file=sys.stderr)
        print(f"Example: --share-dir /Users/myuser/projects:hostshare", file=sys.stderr)
        return None, None

    parts = share_dir_arg.rsplit(':', 1)
    if len(parts) != 2:
        print(f"Error: Invalid --share-dir format: {share_dir_arg}", file=sys.stderr)
        return None, None

    host_path, mount_tag = parts

    # Validate host path exists
    if not os.path.exists(host_path):
        print(f"Error: Host directory does not exist: {host_path}", file=sys.stderr)
        return None, None

    if not os.path.isdir(host_path):
        print(f"Error: Host path is not a directory: {host_path}", file=sys.stderr)
        return None, None

    # Validate mount tag (alphanumeric and underscore only)
    if not re.match(r'^[a-zA-Z0-9_]+$', mount_tag):
        print(f"Error: Mount tag must be alphanumeric: {mount_tag}", file=sys.stderr)
        print(f"Valid examples: hostshare, myfiles, shared_docs", file=sys.stderr)
        return None, None

    # Convert to absolute path
    host_path = os.path.abspath(host_path)

    return host_path, mount_tag


def create_and_run_uefi_with_automation(base_args, config):
    """
    Creates a temporary startup.nsh to automate UEFI boot and runs QEMU.
    """
    bootloader_name, bootloader_script_path = find_uefi_bootloader(config['seven_zip_executable'], config['cdrom'])

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
                subprocess.run(
                    [
                        config["seven_zip_executable"], "e", config["cdrom"],
                        "install.a64/vmlinuz", "install.a64/initrd.gz",
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
    uefi_args = [
        "-drive", f"if=pflash,format=raw,readonly=on,file={config['uefi_code']}",
        "-drive", f"if=pflash,format=raw,file={config['uefi_vars']}",
    ]

    final_args = list(base_args)
    final_args.extend(uefi_args)

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
            config['graphics_device'] = 'virtio-gpu-pci'
        final_args.extend([
            "-device", config["graphics_device"],
            "-display", config["display_type"],
            "-device", config["keyboard_device"],
            "-device", config["mouse_device"],
        ])

    boot_device = config.get("boot_from") or ('cdrom' if config["cdrom"] else 'hd')
    boot_order = 'd' if boot_device == 'cdrom' else 'c'
    final_args.extend(["-boot", f"order={boot_order}"])

    if boot_device == 'cdrom' and config["cdrom"]:
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
        description="Launch a QEMU AArch64 virtual machine with fully independent display and firmware options.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        epilog="""
Directory Sharing:
  Use --share-dir to share a host directory with the guest via VirtFS (9P).

  Example: --share-dir /Users/myuser/projects:hostshare

  Inside the guest, mount with:
    sudo mkdir -p /mnt/hostshare
    sudo mount -t 9p -o trans=virtio,version=9p2000.L hostshare /mnt/hostshare

  To make it persistent, add to /etc/fstab in the guest:
    hostshare  /mnt/hostshare  9p  trans=virtio,version=9p2000.L,_netdev  0  0

  Note: Guest kernel must have 9P filesystem support (CONFIG_9P_FS).
        """
    )

    # --- Argument Definitions ---
    parser.add_argument("--disk-image", required=True, help="Path to the primary virtual hard disk image (.qcow2).")
    parser.add_argument("--cdrom", help="Path to a bootable ISO file (for installation).")
    parser.add_argument(
        "--boot-from", choices=['cdrom', 'hd'],
        help="Specify the boot device for UEFI mode. If --cdrom is used, the default is 'cdrom'. Otherwise, the default is 'hd'."
    )
    parser.add_argument(
        "--firmware", choices=['uefi', 'bios'],
        help="Specify firmware mode. 'uefi' (default) uses firmware to boot. 'bios' uses direct kernel boot."
    )
    parser.add_argument(
        "--console", choices=['gui', 'text'], default='gui',
        help="Choose the console type. 'gui' for a graphical window, 'text' for an integrated serial console with key translation."
    )
    parser.add_argument(
        "--share-dir",
        help="Share a host directory with the guest. Format: /host/path:mount_tag (e.g., /Users/me/data:hostshare). "
             "Mount in guest with: sudo mount -t 9p -o trans=virtio mount_tag /mnt/mountpoint"
    )

    # Executable paths
    parser.add_argument("--qemu-executable", default=QEMU_EXECUTABLE, help="Path to the QEMU binary.")
    parser.add_argument("--seven-zip-executable", default=SEVEN_ZIP_EXECUTABLE, help="Path to the 7z binary.")
    parser.add_argument("--brew-executable", default=BREW_EXECUTABLE, help="Path to the Homebrew binary.")

    # VM configuration
    parser.add_argument("--machine-type", default=MACHINE_TYPE, help="QEMU machine type.")
    parser.add_argument("--accelerator", default=ACCELERATOR, help="VM accelerator to use.")
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
    config = vars(args)

    # --- Post-processing and Validation ---
    if not os.path.exists(config["disk_image"]):
        print(f"Error: Disk image not found: {config['disk_image']}", file=sys.stderr)
        sys.exit(1)
    if config["cdrom"] and not os.path.exists(config["cdrom"]):
        print(f"Error: CD-ROM image not found: {config['cdrom']}", file=sys.stderr)
        sys.exit(1)

    config['firmware'] = config.get('firmware') or detect_firmware_type(config.get('cdrom'))

    if config['firmware'] == 'uefi':
        qemu_prefix = get_qemu_prefix(config['brew_executable'])
        if not config["uefi_code"]:
            config["uefi_code"] = str(qemu_prefix / "share/qemu/edk2-aarch64-code.fd")
        if not config["uefi_vars"]:
            disk_path = Path(config["disk_image"])
            disk_stem = disk_path.stem
            vars_filename = f"{disk_stem}--persistent-variables.fd"
            config["uefi_vars"] = str(disk_path.parent / vars_filename)
        prepare_uefi_vars_file(config["uefi_vars"], config["uefi_code"])

    build_qemu_args(config)

if __name__ == "__main__":
    main()
