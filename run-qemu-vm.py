#!/usr/bin/env python3

#
# Description:
#   This script launches a QEMU virtual machine to install or run an AArch64
#   (ARM64) operating system from a provided ISO file.
#
#   It automates the process of constructing the complex qemu-system-aarch64
#   command by defining each argument as a configurable variable. All settings,
#   such as memory, CPU cores, disk images, and firmware paths, can be
#   overridden using command-line flags.
#
# Usage:
#   1. To start an OS installation from an ISO:
#      ./run-qemu-vm.py --disk-image my-os.qcow2 --cdrom path/to/installer.iso
#
#   2. To run the installed system (without the CD-ROM):
#      ./run-qemu-vm.py --disk-image my-os.qcow2
#
#   3. To see all available options:
#      ./run-qemu-vm.py --help
#
# Prerequisites:
#   - QEMU must be installed (e.g., via `brew install qemu`).
#   - The necessary disk image and ISO files must exist at the specified paths.
#   - The script will create the UEFI variables file if it doesn't exist.
#

import argparse
import subprocess
import sys
import os
from pathlib import Path

# --- Global Configuration & Executable Paths ---
# These variables define the default settings for the VM and can be overridden
# by command-line arguments.

# Path to the Homebrew executable.
BREW_EXECUTABLE = "brew"
# The QEMU binary to execute.
QEMU_EXECUTABLE = "qemu-system-aarch64"
# The machine type to emulate. 'virt' is a modern, versatile virtual platform.
# 'highmem=off' is often not needed with modern QEMU and can cause memory address issues.
MACHINE_TYPE = "virt"
# The accelerator to use. 'hvf' enables macOS's native Hypervisor Framework for speed.
ACCELERATOR = "hvf"
# The CPU model to emulate. 'host' passes through the host CPU features for best performance.
CPU_MODEL = "host"
# The amount of RAM to allocate to the virtual machine (e.g., '4G', '8192M').
MEMORY = "4G"
# The number of CPU cores to assign to the VM.
SMP_CORES = 4
# Path to the AArch64 UEFI firmware file. If None, it will be auto-detected.
UEFI_CODE_PATH = None
# Path to a file for storing UEFI variables (like boot order).
UEFI_VARS_PATH = "qemu-vars.fd"
# The virtual graphics card device. 'virtio-gpu-pci' is a standard, high-performance choice.
GRAPHICS_DEVICE = "virtio-gpu-pci"
# Configures the display window. 'default' creates a standard window, and 'show-cursor=on' makes the cursor visible.
DISPLAY_TYPE = "default,show-cursor=on"
# A virtual USB 3.0 (XHCI) controller.
USB_CONTROLLER = "qemu-xhci"
# A virtual USB keyboard, providing standard keyboard input.
KEYBOARD_DEVICE = "usb-kbd"
# A virtual USB tablet device, providing more accurate mouse pointer tracking.
MOUSE_DEVICE = "usb-tablet"
# Defines a user-mode network backend for internet access.
NETWORK_BACKEND = "user,id=net0"
# Defines the virtual network interface card (NIC) for the VM.
NETWORK_DEVICE = "virtio-net-pci,netdev=net0"

def get_qemu_prefix(brew_executable):
    """Finds the Homebrew installation prefix for QEMU."""
    try:
        prefix = subprocess.check_output(
            [brew_executable, "--prefix", "qemu"],
            text=True,
            stderr=subprocess.PIPE
        ).strip()
        return Path(prefix)
    except (subprocess.CalledProcessError, FileNotFoundError):
        print(
            f"Error: Could not find QEMU prefix using '{brew_executable}'. "
            "Is Homebrew installed and is the executable path correct?",
            file=sys.stderr
        )
        sys.exit(1)

def build_qemu_args(config):
    """Constructs the list of arguments for the QEMU command from the config."""
    qemu_prefix = get_qemu_prefix(config['brew_executable'])

    # Resolve the UEFI code path if it's not explicitly set
    uefi_code_path = config["uefi_code"] or str(qemu_prefix / "share/qemu/edk2-aarch64-code.fd")

    args = [
        config["qemu_executable"],
        "-M", config["machine_type"],
        "-accel", config["accelerator"],
        "-cpu", config["cpu_model"],
        "-m", config["memory"],
        "-smp", str(config["smp_cores"]),
        "-drive", f"if=pflash,format=raw,readonly=on,file={uefi_code_path}",
        "-drive", f"if=pflash,format=raw,file={config['uefi_vars']}",
        "-device", config["graphics_device"],
        "-display", config["display_type"],
        "-device", config["usb_controller"],
        "-device", config["keyboard_device"],
        "-device", config["mouse_device"],
        "-netdev", config["network_backend"],
        "-device", config["network_device"],
        "-hda", config["disk_image"],
    ]

    if config["cdrom"]:
        args.extend(["-cdrom", config["cdrom"]])

    return args

def create_uefi_vars_file(path):
    """Creates an empty file for UEFI variables if it doesn't exist."""
    if not os.path.exists(path):
        print(f"Creating UEFI variables file: {path}")
        Path(path).touch()

def run_qemu(args):
    """Executes the QEMU command with the provided arguments."""
    print("--- Starting QEMU with the following command ---")
    print(subprocess.list2cmdline(args))
    print("-------------------------------------------------")
    try:
        subprocess.run(args, check=True)
    except FileNotFoundError:
        print(f"Error: Command not found: {args[0]}", file=sys.stderr)
        print("Please ensure QEMU is installed and the executable path is correct.", file=sys.stderr)
        sys.exit(1)
    except subprocess.CalledProcessError as e:
        print(f"QEMU exited with an error (code {e.returncode}).", file=sys.stderr)
        sys.exit(e.returncode)

def main():
    """Parses command-line arguments and launches the VM."""
    parser = argparse.ArgumentParser(
        description="Launch a QEMU AArch64 virtual machine.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )

    # --- Argument Definitions ---
    # Required arguments
    parser.add_argument("--disk-image", required=True, help="Path to the primary virtual hard disk image (.qcow2).")

    # Optional arguments with defaults from global variables
    parser.add_argument("--cdrom", help="Path to a bootable ISO file (for installation).")

    # Executable paths
    parser.add_argument("--qemu-executable", default=QEMU_EXECUTABLE, help="Path to the QEMU binary.")
    parser.add_argument("--brew-executable", default=BREW_EXECUTABLE, help="Path to the Homebrew binary.")

    # VM configuration
    parser.add_argument("--machine-type", default=MACHINE_TYPE, help="QEMU machine type.")
    parser.add_argument("--accelerator", default=ACCELERATOR, help="VM accelerator to use.")
    parser.add_argument("--cpu-model", default=CPU_MODEL, help="CPU model to emulate.")
    parser.add_argument("--memory", default=MEMORY, help="RAM to allocate to the VM.")
    parser.add_argument("--smp-cores", type=int, default=SMP_CORES, help="Number of CPU cores for the VM.")
    parser.add_argument("--uefi-code", default=UEFI_CODE_PATH, help="Path to UEFI firmware code. (Default: auto-detected)")
    parser.add_argument("--uefi-vars", default=UEFI_VARS_PATH, help="Path to UEFI variables file.")
    parser.add_argument("--graphics-device", default=GRAPHICS_DEVICE, help="Virtual graphics device.")
    parser.add_argument("--display-type", default=DISPLAY_TYPE, help="QEMU display configuration.")
    parser.add_argument("--usb-controller", default=USB_CONTROLLER, help="Virtual USB controller.")
    parser.add_argument("--keyboard-device", default=KEYBOARD_DEVICE, help="Virtual keyboard device.")
    parser.add_argument("--mouse-device", default=MOUSE_DEVICE, help="Virtual mouse/tablet device.")
    parser.add_argument("--network-backend", default=NETWORK_BACKEND, help="Network backend configuration.")
    parser.add_argument("--network-device", default=NETWORK_DEVICE, help="Virtual network interface.")

    try:
        args = parser.parse_args()
    except argparse.ArgumentError as e:
        parser.print_usage(sys.stderr)
        print(str(e), file=sys.stderr)
        sys.exit(1)

    # Convert parsed arguments to a dictionary for easier handling
    config = vars(args)

    # Ensure required files exist before attempting to launch
    create_uefi_vars_file(config["uefi_vars"])
    if not os.path.exists(config["disk_image"]):
        print(f"Error: Disk image not found at '{config['disk_image']}'", file=sys.stderr)
        sys.exit(1)
    if config["cdrom"] and not os.path.exists(config["cdrom"]):
        print(f"Error: CD-ROM ISO not found at '{config['cdrom']}'", file=sys.stderr)
        sys.exit(1)

    qemu_args = build_qemu_args(config)
    run_qemu(qemu_args)

if __name__ == "__main__":
    main()
