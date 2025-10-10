#!/usr/bin/env python3

#
# Description:
#   This script launches a QEMU virtual machine to install or run an AArch64
#   (ARM64) operating system. It intelligently selects the required firmware
#   (UEFI or BIOS) and a compatible display (graphical or serial console)
#   based on the type of ISO provided.
#
# Usage:
#   1. To start an OS installation from a modern UEFI ISO (graphical):
#      ./run-qemu-vm.py --disk-image my-os.qcow2 --cdrom uefi-installer.iso
#
#   2. To start an OS installation from a legacy BIOS ISO (serial console):
#      ./run-qemu-vm.py --disk-image my-os.qcow2 --cdrom bios-installer.iso
#      (This requires the 'p7zip' package to be installed, e.g., 'brew install p7zip')
#
#   3. To run an installed system (defaults to UEFI, graphical):
#      ./run-qemu-vm.py --disk-image my-os.qcow2
#
#   4. To see all available options:
#      ./run-qemu-vm.py --help
#
# Prerequisites:
#   - QEMU must be installed (e.g., via `brew install qemu`).
#   - The `file` command-line utility must be available.
#   - For BIOS/serial installations, '7z' must be in the PATH (from 'p7zip').
#   - The necessary disk image and ISO files must exist at the specified paths.
#

import argparse
import subprocess
import sys
import os
import shutil
import tempfile
from pathlib import Path
import re

# --- Global Configuration & Executable Paths ---
# These variables define the default settings for the VM and can be overridden
# by command-line arguments.

# Path to the Homebrew executable.
BREW_EXECUTABLE = "brew"
# The QEMU binary to execute.
QEMU_EXECUTABLE = "qemu-system-aarch64"
# The 7-Zip binary to execute for extracting files from ISOs.
SEVEN_ZIP_EXECUTABLE = "7z"
# The machine type to emulate. 'virt' is a modern, versatile virtual platform.
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
# Path to a file for storing UEFI variables. If None, it defaults to being
# named after and located next to the disk image.
UEFI_VARS_PATH = None
# The virtual graphics card device. This is now determined dynamically.
GRAPHICS_DEVICE = None
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

    # The 'MBR boot sector' check is incorrect for AArch64 UEFI ISOs.
    # We now default to 'uefi' unless a convention like 'bios' in the
    # filename indicates a special direct-boot image.
    if 'bios' in Path(iso_path).name.lower():
         print("Info: ISO filename suggests BIOS/direct-kernel boot. Switching to serial console.")
         return 'bios'

    return 'uefi'

def find_uefi_bootloader(seven_zip_executable, iso_path):
    """Inspects an ISO to find the AArch64 UEFI bootloader file."""
    print(f"Info: Searching for UEFI bootloader in '{iso_path}'...")
    try:
        # Use '7z l' to list all files, making the process case-insensitive.
        result = subprocess.run(
            [seven_zip_executable, 'l', iso_path],
            capture_output=True, text=True, check=True
        )
    except (FileNotFoundError, subprocess.CalledProcessError) as e:
        print(f"Warning: Could not list files in ISO with '{seven_zip_executable}'. Cannot automate boot.", file=sys.stderr)
        if hasattr(e, 'stderr'):
            print(e.stderr, file=sys.stderr)
        return None, None

    # Dynamically find the full path to the AArch64 bootloader.
    # This is more robust against case-sensitivity issues (e.g., 'efi/boot' vs 'EFI/BOOT').
    bootloader_full_path = None
    for line in result.stdout.splitlines():
        # Match lines that look like file entries and contain the bootloader name
        if "bootaa64.efi" in line.lower():
            # The path is usually the last column in the '7z l' output.
            parts = line.split()
            if len(parts) > 0 and parts[-1].lower().endswith('bootaa64.efi'):
                 # Check if the path contains 'efi' and 'boot' components to be sure
                 if 'efi' in parts[-1].lower() and 'boot' in parts[-1].lower():
                    bootloader_full_path = parts[-1]
                    break

    if bootloader_full_path:
        # The bootloader name is the final component of the path.
        p = Path(bootloader_full_path)
        bootloader_name = p.name
        # The path for the startup.nsh script needs backslashes.
        bootloader_script_path = str(p).replace('/', '\\')
        print(f"Info: Found UEFI bootloader: {bootloader_name} at path {bootloader_full_path}")
        return bootloader_name, bootloader_script_path

    print("Warning: Could not find 'bootaa64.efi' in the ISO. Automatic boot may fail.", file=sys.stderr)
    return None, None


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
                "# Switch to the first filesystem (the CD-ROM)\n"
                "FS0:\n"
                "# Execute the bootloader\n"
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

            run_qemu(automated_args)
    else:
        print("Warning: Proceeding without boot automation script.", file=sys.stderr)
        run_qemu(base_args)

def build_qemu_args(config):
    """Constructs the list of arguments for the QEMU command from the config."""
    args = [
        config["qemu_executable"],
        "-M", config["machine_type"],
        "-accel", config["accelerator"],
        "-cpu", config["cpu_model"],
        "-m", config["memory"],
        "-smp", str(config["smp_cores"]),
        "-netdev", config["network_backend"],
        "-device", config["network_device"],
        "-hda", config["disk_image"],
    ]

    if config["cdrom"]:
        args.extend(["-cdrom", config["cdrom"]])

    if config['firmware'] == 'bios':
        if not config["cdrom"]:
            print("Error: --cdrom is required for BIOS installation mode.", file=sys.stderr)
            sys.exit(1)

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
            except FileNotFoundError:
                print(
                    f"Error: Command '{config['seven_zip_executable']}' not found.",
                    file=sys.stderr
                )
                print("Please install p7zip (e.g., 'brew install p7zip') and ensure '7z' is in your PATH.", file=sys.stderr)
                sys.exit(1)
            except subprocess.CalledProcessError as e:
                print("Error: Failed to extract boot files from the ISO.", file=sys.stderr)
                print(f"7z stdout: {e.stdout}", file=sys.stderr)
                print(f"7z stderr: {e.stderr}", file=sys.stderr)
                sys.exit(1)

            kernel_path = os.path.join(temp_dir, "vmlinuz")
            initrd_path = os.path.join(temp_dir, "initrd.gz")

            if not (os.path.exists(kernel_path) and os.path.exists(initrd_path)):
                print(f"Error: Could not find 'vmlinuz' or 'initrd.gz' after extraction.", file=sys.stderr)
                print(f"Please check that the ISO '{config['cdrom']}' contains 'install.a64/vmlinuz' and 'install.a64/initrd.gz'.", file=sys.stderr)
                sys.exit(1)

            args.extend([
                "-nographic",
                "-monitor", "null",
                "-serial", "stdio",
                "-kernel", kernel_path,
                "-initrd", initrd_path,
                "-append", "console=ttyAMA0"
            ])
            run_qemu(args)

    else: # uefi mode
        args.extend([
            "-device", config["graphics_device"],
            "-display", config["display_type"],
            "-device", config["usb_controller"],
            "-device", config["keyboard_device"],
            "-device", config["mouse_device"],
        ])

        args.extend([
            "-drive", f"if=pflash,format=raw,readonly=on,file={config['uefi_code']}",
            "-drive", f"if=pflash,format=raw,file={config['uefi_vars']}",
        ])

        boot_device = config.get("boot_from") or ('cdrom' if config["cdrom"] else 'hd')
        boot_order = 'd' if boot_device == 'cdrom' else 'c'
        args.extend(["-boot", f"order={boot_order}"])

        if boot_device == 'cdrom' and config["cdrom"]:
            create_and_run_uefi_with_automation(args, config)
        else:
            run_qemu(args)

def prepare_uefi_vars_file(vars_path, code_path):
    """
    Ensures the UEFI variables file is valid.
    If it doesn't exist or has the wrong size, it's (re)created by copying
    the UEFI code file. Otherwise, it's left untouched to preserve its state.
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

def run_qemu(args):
    """Executes the QEMU command and handles interrupts."""
    print("--- Starting QEMU with the following command ---")
    print(subprocess.list2cmdline(args))
    print("-------------------------------------------------")
    print(">>> QEMU output will be directed to this terminal. To exit, press Ctrl-A then X. <<<")
    try:
        process = subprocess.Popen(args)
        process.wait()
    except FileNotFoundError:
        print(f"\nError: Command not found: {args[0]}", file=sys.stderr)
        print("Please ensure QEMU is installed and the executable path is correct.", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nKeyboardInterrupt received. Exiting.", file=sys.stderr)
        if 'process' in locals() and process.poll() is None:
            process.terminate()
        sys.exit(130)

    if 'process' in locals() and process.returncode != 0:
        print(f"\nQEMU exited with an error (code {process.returncode}).", file=sys.stderr)
        sys.exit(process.returncode)

def main():
    """Parses command-line arguments and launches the VM."""
    parser = argparse.ArgumentParser(
        description="Launch a QEMU AArch64 virtual machine.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
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
        help="Specify firmware mode. If not provided, it's auto-detected based on the CD-ROM, defaulting to 'uefi'."
    )

    # Executable paths
    parser.add_argument("--qemu-executable", default=QEMU_EXECUTABLE, help="Path to the QEMU binary.")
    parser.add_argument("--seven-zip-executable", default=SEVEN_ZIP_EXECUTABLE, help="Path to the 7z binary.")
    parser.add_argument("--brew-executable", default=BREW_EXECUTABLE, help="Path to the Homebrew binary.")

    # VM configuration
    # ... (rest of the arguments are unchanged)
    parser.add_argument("--machine-type", default=MACHINE_TYPE, help="QEMU machine type.")
    parser.add_argument("--accelerator", default=ACCELERATOR, help="VM accelerator to use.")
    parser.add_argument("--cpu-model", default=CPU_MODEL, help="CPU model to emulate.")
    parser.add_argument("--memory", default=MEMORY, help="RAM to allocate to the VM.")
    parser.add_argument("--smp-cores", type=int, default=SMP_CORES, help="Number of CPU cores for the VM.")
    parser.add_argument("--uefi-code", default=UEFI_CODE_PATH, help="Path to UEFI firmware code. (Default: auto-detected)")
    parser.add_argument("--uefi-vars", default=UEFI_VARS_PATH, help="Path to UEFI variables file. Defaults to a descriptive name next to the disk image.")
    parser.add_argument("--graphics-device", default=GRAPHICS_DEVICE, help="Virtual graphics device. Auto-selected based on firmware if not specified.")
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

    config = vars(args)

    # --- Post-processing and Validation ---
    if not os.path.exists(config["disk_image"]):
        print(f"Error: Disk image not found at '{config['disk_image']}'", file=sys.stderr)
        sys.exit(1)
    if config["cdrom"] and not os.path.exists(config["cdrom"]):
        print(f"Error: CD-ROM ISO not found at '{config['cdrom']}'", file=sys.stderr)
        sys.exit(1)

    # Determine firmware mode: user override or auto-detect
    config['firmware'] = config.get('firmware') or detect_firmware_type(config.get('cdrom'))

    # Set graphics device for UEFI mode if not explicitly set by user
    if config['firmware'] == 'uefi' and not config['graphics_device']:
        config['graphics_device'] = 'virtio-gpu-pci'

    # If in UEFI mode, prepare the necessary files
    if config['firmware'] == 'uefi':
        qemu_prefix = get_qemu_prefix(config['brew_executable'])
        # Resolve UEFI code path if not provided
        if not config["uefi_code"]:
            config["uefi_code"] = str(qemu_prefix / "share/qemu/edk2-aarch64-code.fd")

        # Resolve UEFI vars path if not provided by the user
        if not config["uefi_vars"]:
            disk_path = Path(config["disk_image"])
            disk_stem = disk_path.stem
            vars_filename = f"{disk_stem}--persistent-variables.fd"
            config["uefi_vars"] = str(disk_path.parent / vars_filename)

        # Ensure the UEFI variables file is valid before launching
        prepare_uefi_vars_file(config["uefi_vars"], config["uefi_code"])

    # The build_qemu_args function now calls run_qemu internally.
    build_qemu_args(config)

if __name__ == "__main__":
    main()
