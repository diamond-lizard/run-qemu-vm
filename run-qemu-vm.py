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
import threading
import platform

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
# The hardware virtualization framework to use; will be auto-detected.
ACCELERATOR = None
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

# --- Serial Console Configuration ---
SERIAL_DEVICE_PROFILES = {
    "pc": {
        "args": ["-device", "isa-serial,chardev=char0"],
        "description": "Standard PC serial port (ISA/16550A). Expected by most x86 OSes."
    },
    "pl011": {
        "args": ["-serial", "chardev:char0"],
        "description": "ARM PL011 serial port. Standard for ARM 'virt' machines."
    },
    "virtio": {
        "args": ["-device", "virtio-serial-pci", "-device", "virtconsole,chardev=char0"],
        "description": "Virtio paravirtualized serial port. High-performance, requires guest drivers."
    }
}

ARCH_DEFAULT_SERIAL = {
    "x86_64": "pc",
    "i386": "pc",
    "aarch64": "pl011",
    "riscv64": "pl011", # The 'virt' machine for RISC-V also uses a PL011-compatible UART
    "default": "pl011"
}


# --- Network Configuration ---

# The network backend mode for QEMU user-mode networking (SLIRP/NAT).
NETWORK_MODE = "user"
NETWORK_ID = "net0"
NETWORK_PORT_FORWARDS = "hostfwd=tcp::2222-:22"
NETWORK_BACKEND = f"{NETWORK_MODE},id={NETWORK_ID}" + (f",{NETWORK_PORT_FORWARDS}" if NETWORK_PORT_FORWARDS else "")
NETWORK_DEVICE = f"virtio-net-pci,netdev={NETWORK_ID}"

# --- Directory Sharing Configuration ---
VIRTFS_SECURITY_MODEL = "mapped-xattr"
VIRTFS_VERSION = "9p2000.L"
MOUNT_TAG_PATTERN = r'^[a-zA-Z0-9_]+$'
MOUNT_TAG_ALLOWED_CHARS = "letters (a-z, A-Z), numbers (0-9), and underscores (_)"

# --- Text Console Mode Constants ---
MODE_SERIAL_CONSOLE = 'serial_console'
MODE_CONTROL_MENU = 'control_menu'
MODE_QEMU_MONITOR = 'qemu_monitor'

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
    if not patterns: return None, None
    print(f"Info: Searching for UEFI bootloader in '{iso_path}' for {architecture}...")
    try:
        result = subprocess.run([seven_zip_executable, 'l', iso_path], capture_output=True, text=True, check=True)
    except (FileNotFoundError, subprocess.CalledProcessError) as e:
        print(f"Warning: Could not list files in ISO with '{seven_zip_executable}'. Error: {e}", file=sys.stderr)
        return None, None

    for line in result.stdout.splitlines():
        for pattern in patterns:
            if pattern in line.lower():
                parts = line.split()
                if len(parts) > 0 and parts[-1].lower().endswith(pattern):
                    bootloader_full_path = parts[-1]
                    p = Path(bootloader_full_path)
                    print(f"Info: Found UEFI bootloader: {p.name} at path {bootloader_full_path}")
                    return p.name, str(p).replace('/', '\\')
    print(f"Warning: Could not find a suitable bootloader ({', '.join(patterns)}) in the ISO.", file=sys.stderr)
    return None, None

def inspect_iso_for_boot_strategy(seven_zip_executable, iso_path):
    """
    Inspects the ISO file listing to determine the best boot strategy.
    Returns 'trust_firmware' for complex bootloaders (like GRUB) or
    'direct_efi_boot' for simpler cases.
    """
    if not iso_path:
        return 'direct_efi_boot' # No ISO, so doesn't matter

    print(f"Info: Inspecting ISO '{Path(iso_path).name}' for bootloader configuration...")
    try:
        result = subprocess.run([seven_zip_executable, 'l', iso_path], capture_output=True, text=True, check=True, encoding='utf-8', errors='ignore')
    except (FileNotFoundError, subprocess.CalledProcessError) as e:
        print(f"Warning: Could not inspect ISO. Defaulting to firmware boot. Error: {e}", file=sys.stderr)
        return 'trust_firmware'

    # Clues that indicate a complex bootloader that should be trusted
    complex_boot_clues = [
        'boot/grub/grub.cfg',
        'isolinux/syslinux.cfg',
        'boot/syslinux/syslinux.cfg',
    ]

    iso_content = result.stdout.lower()
    for clue in complex_boot_clues:
        if clue in iso_content:
            print(f"Info: Found '{clue}'. Recommending 'trust_firmware' boot strategy.")
            return 'trust_firmware'

    print("Info: No complex bootloader config found. Recommending 'direct_efi_boot' strategy.")
    return 'direct_efi_boot'


def parse_share_dir_argument(share_dir_arg):
    """Parse and validate the --share-dir argument."""
    if not share_dir_arg: return None, None
    if ':' not in share_dir_arg:
        print("Error: --share-dir format must be '/host/path:mount_tag'", file=sys.stderr)
        sys.exit(1)
    host_path, mount_tag = share_dir_arg.rsplit(':', 1)
    if not os.path.isdir(host_path):
        print(f"Error: Host path is not a directory: {host_path}", file=sys.stderr)
        sys.exit(1)
    if not re.match(MOUNT_TAG_PATTERN, mount_tag):
        print(f"Error: Invalid characters in mount tag '{mount_tag}'. Allowed: {MOUNT_TAG_ALLOWED_CHARS}", file=sys.stderr)
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


class TextConsoleManager:
    """Manages the text console mode with key translation and mode switching."""
    def __init__(self, qemu_process, pty_device, monitor_socket):
        self.qemu_process, self.pty_device, self.monitor_socket_path = qemu_process, pty_device, monitor_socket
        self.pty_fd, self.monitor_sock, self.original_settings = None, None, None
        self.current_mode = MODE_SERIAL_CONSOLE
        self.stdin_fd, self.stdout_fd = sys.stdin.fileno(), sys.stdout.fileno()

    def setup(self):
        try:
            for i in range(20):
                try:
                    self.pty_fd = os.open(self.pty_device, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
                    if i > 0: print(f"Info: PTY device '{self.pty_device}' opened successfully after {i+1} attempts.")
                    break
                except FileNotFoundError:
                    if i < 19:
                        if i == 0: print(f"Info: PTY device '{self.pty_device}' not yet available, retrying...", file=sys.stdout)
                        time.sleep(0.1)
                    else: raise

            attrs = termios.tcgetattr(self.pty_fd)
            attrs[0] = attrs[1] = attrs[3] = 0
            attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
            termios.tcsetattr(self.pty_fd, termios.TCSANOW, attrs)

            self.monitor_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            for _ in range(20):
                try: self.monitor_sock.connect(self.monitor_socket_path); break
                except (FileNotFoundError, ConnectionRefusedError): time.sleep(0.1)
            else: print("Warning: Could not connect to QEMU monitor", file=sys.stderr)

            self.monitor_sock.setblocking(False)
            try:
                while self.monitor_sock.recv(4096): pass
            except BlockingIOError: pass

            self.original_settings = termios.tcgetattr(self.stdin_fd)
            tty.setraw(self.stdin_fd)
            print(f"\nConnected to serial console: {self.pty_device}\nPress Ctrl-] for control menu.\n", flush=True)
            return True
        except Exception as e:
            print(f"Error setting up text console: {e}", file=sys.stderr)
            return False

    def restore_terminal(self):
        if self.original_settings: termios.tcsetattr(self.stdin_fd, termios.TCSADRAIN, self.original_settings)

    def show_control_menu(self):
        self.restore_terminal()
        print("\n╔════════════════════════════════════╗\n║ run-qemu-vm.py Control Menu        ║\n╠════════════════════════════════════╣\n║ q - Quit QEMU and exit             ║\n║ m - Enter QEMU monitor             ║\n║ r - Resume serial console          ║\n╚════════════════════════════════════╝\nChoice: ", end='', flush=True)
        choice = sys.stdin.read(1)
        if choice.lower() == 'q': self.quit_qemu(); return False
        self.current_mode = MODE_QEMU_MONITOR if choice.lower() == 'm' else MODE_SERIAL_CONSOLE
        if self.current_mode == MODE_QEMU_MONITOR: print("\nEntering QEMU monitor (Ctrl-] for menu)... \n(qemu) ", end='', flush=True)
        else: print("\nResuming serial console...", flush=True)
        tty.setraw(self.stdin_fd)
        return True

    def quit_qemu(self):
        print("\nShutting down VM...", flush=True)
        try:
            self.monitor_sock.send(b"quit\n")
            self.qemu_process.wait(timeout=5)
        except: self.qemu_process.kill()
        print("VM stopped.")

    def run(self):
        try:
            while self.qemu_process.poll() is None:
                if self.current_mode == MODE_SERIAL_CONSOLE:
                    readable, _, _ = select.select([self.stdin_fd, self.pty_fd], [], [], 0.1)
                    if self.stdin_fd in readable:
                        data = os.read(self.stdin_fd, 1024)
                        if b'\x1d' in data:
                            if not self.show_control_menu(): break
                        else: os.write(self.pty_fd, data.replace(b'\x7f', b'\x08'))
                    if self.pty_fd in readable and (data := os.read(self.pty_fd, 4096)): os.write(self.stdout_fd, data)
                elif self.current_mode == MODE_QEMU_MONITOR:
                    readable, _, _ = select.select([self.stdin_fd, self.monitor_sock], [], [], 0.1)
                    if self.stdin_fd in readable:
                        data = os.read(self.stdin_fd, 1024)
                        if b'\x1d' in data:
                            if not self.show_control_menu(): break
                        else: self.monitor_sock.send(data)
                    if self.monitor_sock in readable and (data := self.monitor_sock.recv(4096)): os.write(self.stdout_fd, data)
        except (OSError, KeyboardInterrupt) as e:
            if isinstance(e, KeyboardInterrupt): print("\n\nInterrupted. Shutting down...")
            else: print(f"\nConsole error: {e}", file=sys.stderr)
            self.quit_qemu()
        finally:
            self.restore_terminal()
            if self.pty_fd: os.close(self.pty_fd)
            if self.monitor_sock: self.monitor_sock.close()
            return self.qemu_process.returncode or 0

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
    if config["cdrom"]: args.extend(["-cdrom", config["cdrom"]])
    if config.get("share_dir"):
        host_path, mount_tag = parse_share_dir_argument(config["share_dir"])
        if host_path and mount_tag:
            args.extend(["-virtfs", f"local,path={host_path},mount_tag={mount_tag},security_model={VIRTFS_SECURITY_MODEL},id={mount_tag}"])
        else: sys.exit(1)

    # --- Firmware Selection ---
    # Default to UEFI, but switch to BIOS for x86 text mode as it's more likely to have serial-by-default bootloaders.
    is_x86_text_mode = config['console'] == 'text' and config['architecture'] in ['x86_64', 'i386']

    if config['firmware'] == 'uefi' and not is_x86_text_mode:
        if config.get('uefi_code') and config.get('uefi_vars'):
            print("Info: Using UEFI boot.")
            args.extend(["-drive", f"if=pflash,format=raw,readonly=on,file={config['uefi_code']}", "-drive", f"if=pflash,format=raw,file={config['uefi_vars']}"])
    else:
        if is_x86_text_mode:
            print("Info: Forcing Legacy BIOS boot for x86 text mode to enable serial console.")
        else:
            print("Info: Using Legacy BIOS boot.")

    if config['console'] == 'text':
        monitor_socket = f"/tmp/qemu-monitor-{os.getpid()}.sock"
        config['monitor_socket'] = monitor_socket
        args.extend(["-monitor", f"unix:{monitor_socket},server,nowait", "-chardev", "pty,id=char0"])

        serial_profile_key = config.get("serial_device") or ARCH_DEFAULT_SERIAL.get(config['architecture'], ARCH_DEFAULT_SERIAL['default'])
        serial_args = SERIAL_DEVICE_PROFILES[serial_profile_key]['args']
        print(f"Info: Using '{serial_profile_key}' serial profile.")
        args.extend(serial_args)
        args.append("-nographic")
    else: # gui
        gfx = config['graphics_device'] or ('virtio-gpu-pci' if config['architecture'] in ['x86_64', 'aarch64'] else 'VGA')
        args.extend(["-device", gfx, "-display", config["display_type"], "-device", config["keyboard_device"], "-device", config["mouse_device"]])

    boot_order = 'd' if config.get("boot_from") == 'cdrom' or (not config.get("boot_from") and config["cdrom"]) else 'c'
    args.extend(["-boot", f"order={boot_order}"])

    # --- Smart Boot Automation (for UEFI GUI mode) ---
    if boot_order == 'd' and config["cdrom"] and config['firmware'] == 'uefi' and not is_x86_text_mode:
        boot_strategy = inspect_iso_for_boot_strategy(config['seven_zip_executable'], config['cdrom'])
        if boot_strategy == 'direct_efi_boot':
            create_and_run_uefi_with_automation(args, config)
        else:
            run_qemu(args, config)
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
    print("--- Starting QEMU with the following command ---\n" + subprocess.list2cmdline(args) + "\n" + "-"*50, flush=True)
    try:
        if config.get('console') == 'text':
            process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
            event, holder = threading.Event(), [None]
            thread = threading.Thread(target=parse_pty_device_from_thread, args=(process, event, holder))
            thread.daemon = True
            thread.start()
            if not event.wait(timeout=10.0) or not holder[0]: # Increased timeout for TCG
                print("Error: Could not find PTY device in QEMU output within 10 seconds.", file=sys.stderr)
                process.kill(); thread.join(); sys.exit(1)
            console = TextConsoleManager(process, holder[0], config['monitor_socket'])
            if not console.setup():
                process.kill(); thread.join(); sys.exit(1)
            return_code = console.run()
            thread.join()
            sys.exit(return_code)
        else:
            process = subprocess.Popen(args)
            process.wait()
            if process.returncode != 0: sys.exit(process.returncode)
    except FileNotFoundError:
        print(f"Error: QEMU executable '{args[0]}' not found.", file=sys.stderr); sys.exit(1)
    except KeyboardInterrupt:
        print("\nInterrupted"); sys.exit(130)

def main():
    """Parses command-line arguments and launches the VM."""
    parser = argparse.ArgumentParser(description="Launch a QEMU virtual machine with flexible options.", formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--architecture", required=True, help="QEMU system architecture (e.g., aarch64, x86_64). Use 'list' for options.")
    parser.add_argument("--disk-image", help="Path to the primary virtual hard disk image (.qcow2).")
    parser.add_argument("--cdrom", help="Path to a bootable ISO file.")
    parser.add_argument("--boot-from", choices=['cdrom', 'hd'], help="Specify boot device for UEFI mode.")
    parser.add_argument("--firmware", choices=['uefi', 'bios'], help="Specify firmware mode.")
    parser.add_argument("--console", choices=['gui', 'text'], default='gui', help="Console type.")
    parser.add_argument("--share-dir", metavar="/HOST/PATH:MOUNT_TAG", help="Share a host directory with the guest via VirtFS.")
    parser.add_argument("--serial-device", help="Serial device profile. Use 'list' to see options for the selected architecture.")

    parser.add_argument("--machine-type", default=MACHINE_TYPE, help="QEMU machine type.")
    parser.add_argument("--accelerator", default=ACCELERATOR, help="VM accelerator (e.g., hvf, kvm, tcg). Default: auto-detected.")
    parser.add_argument("--cpu-model", default=CPU_MODEL, help="CPU model to emulate.")
    parser.add_argument("--memory", default=MEMORY, help="RAM for the VM.")
    parser.add_argument("--smp-cores", type=int, default=SMP_CORES, help="Number of CPU cores.")
    # Suppressed from help as they are auto-detected or internal
    suppressed_args = {
        "qemu_executable": QEMU_EXECUTABLE, "seven_zip_executable": SEVEN_ZIP_EXECUTABLE, "brew_executable": BREW_EXECUTABLE,
        "uefi_code_path": UEFI_CODE_PATH, "uefi_vars_path": UEFI_VARS_PATH, "graphics_device": GRAPHICS_DEVICE,
        "display_type": DISPLAY_TYPE, "usb_controller": USB_CONTROLLER, "keyboard_device": KEYBOARD_DEVICE,
        "mouse_device": MOUSE_DEVICE, "network_backend": NETWORK_BACKEND, "network_device": NETWORK_DEVICE
    }
    for arg, default_val in suppressed_args.items():
        cli_arg = f"--{arg.replace('_', '-')}"
        parser.add_argument(cli_arg, default=default_val, help=argparse.SUPPRESS)


    args = parser.parse_args()
    config = vars(args)

    if args.architecture == 'list':
        print("Available QEMU architectures:\n" + "\n".join(f"  - {arch}" for arch in SUPPORTED_ARCHITECTURES)); sys.exit(0)
    if args.architecture not in SUPPORTED_ARCHITECTURES:
        print(f"Error: Unsupported architecture '{args.architecture}'. Use 'list' for options.", file=sys.stderr); sys.exit(1)
    if not args.disk_image: parser.error("--disk-image is required.")

    if args.serial_device == 'list':
        default_key = ARCH_DEFAULT_SERIAL.get(args.architecture, ARCH_DEFAULT_SERIAL['default'])
        print(f"Available serial device profiles for architecture '{args.architecture}':")
        for key, profile in SERIAL_DEVICE_PROFILES.items():
            is_default = "(default)" if key == default_key else ""
            print(f"  - {key:<10} {is_default:<10} {profile['description']}")
        sys.exit(0)
    if args.serial_device and args.serial_device not in SERIAL_DEVICE_PROFILES:
        print(f"Error: Unknown serial device profile '{args.serial_device}'. Use 'list' for options.", file=sys.stderr); sys.exit(1)

    config['qemu_executable'] = f"qemu-system-{config['architecture']}"
    host_arch, guest_arch = platform.machine(), config['architecture']
    is_native = (host_arch == 'arm64' and guest_arch == 'aarch64') or (host_arch == 'x86_64' and guest_arch == 'x86_64')

    if config['accelerator'] is None:
        if sys.platform == "darwin" and is_native:
            config['accelerator'] = 'hvf'; print("Info: Auto-selected 'hvf' accelerator for native hardware virtualization.")
        else:
            config['accelerator'] = 'tcg'; print("Info: Auto-selected 'tcg' accelerator for emulation.")

    if config['cpu_model'] == 'host' and not is_native:
        config['cpu_model'] = 'max'; print(f"Info: Auto-selected CPU model '{config['cpu_model']}' for emulation.")

    if not os.path.exists(config["disk_image"]): print(f"Error: Disk image not found: {config['disk_image']}", file=sys.stderr); sys.exit(1)
    if config["cdrom"] and not os.path.exists(config["cdrom"]): print(f"Error: CD-ROM image not found: {config['cdrom']}", file=sys.stderr); sys.exit(1)

    config['firmware'] = config.get('firmware') or detect_firmware_type(config.get('cdrom'), config['architecture'])
    if config['architecture'] == 'x86_64' and config['machine_type'] == 'virt': config['machine_type'] = 'q35'

    if config['firmware'] == 'uefi':
        fw_map = {'aarch64': 'edk2-aarch64-code.fd', 'x86_64': 'edk2-x86_64-code.fd', 'riscv64': 'edk2-riscv64-code.fd'}
        if (fw_filename := fw_map.get(config['architecture'])):
            qemu_prefix = get_qemu_prefix(config['brew_executable'])
            if not config["uefi_code_path"]: config["uefi_code_path"] = str(qemu_prefix / "share/qemu" / fw_filename)
            if not config["uefi_vars_path"]:
                disk_path = Path(config["disk_image"])
                config["uefi_vars_path"] = str(disk_path.parent / f"{disk_path.stem}-{config['architecture']}-vars.fd")
            if os.path.exists(config["uefi_code_path"]): prepare_uefi_vars_file(config["uefi_vars_path"], config["uefi_code_path"])
            else: print(f"Warning: UEFI firmware '{fw_filename}' not found. Disabling UEFI.", file=sys.stderr); config['firmware'] = 'bios'
        else: print(f"Info: No standard UEFI for '{config['architecture']}'. Assuming BIOS.", file=sys.stderr); config['firmware'] = 'bios'

    # Rename for consistency before passing to build_qemu_args
    config['uefi_code'] = config.pop('uefi_code_path', None)
    config['uefi_vars'] = config.pop('uefi_vars_path', None)

    build_qemu_args(config)

if __name__ == "__main__":
    main()
