#!/usr/bin/env python3

#
# Description:
#   This script launches a QEMU virtual machine to install or run an AArch64
#   (ARM64) operating system. It intelligently selects the required firmware
#   (UEFI or BIOS), and allows the user to choose between a graphical (GUI)
#   or text-based (serial) console, with all combinations being supported.
#
# Usage:
#   1. UEFI installation with a GUI:
#      ./run-qemu-vm.py --disk-image my-os.qcow2 --cdrom uefi-installer.iso --console gui
#
#   2. UEFI installation with a text-only terminal:
#      ./run-qemu-vm.py --disk-image my-os.qcow2 --cdrom uefi-installer.iso --console text
#
#   3. Direct-kernel (BIOS) installation with a text-only terminal:
#      ./run-qemu-vm.py --disk-image my-os.qcow2 --cdrom bios-installer.iso --firmware bios --console text
#
#   4. Direct-kernel (BIOS) installation with a GUI:
#      ./run-qemu-vm.py --disk-image my-os.qcow2 --cdrom bios-installer.iso --firmware bios --console gui
#
#   5. To see all available options:
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
# The network backend configuration for user-mode networking.
NETWORK_BACKEND = "user,id=net0"
# The virtual network interface card (NIC) device attached to the guest.
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

            run_qemu(automated_args)
    else:
        print("Warning: Proceeding without boot automation script.", file=sys.stderr)
        run_qemu(base_args)

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
                # No extra args needed for GUI BIOS boot

            run_qemu(final_args)
            return

    # --- UEFI Configuration (Default) ---
    uefi_args = [
        "-drive", f"if=pflash,format=raw,readonly=on,file={config['uefi_code']}",
        "-drive", f"if=pflash,format=raw,file={config['uefi_vars']}",
    ]

    final_args = list(base_args)
    final_args.extend(uefi_args)

    if config['console'] == 'text':
        print("Info: Using text-only (serial) console via pseudo-terminal (pty).")
        final_args.extend([
            "-nographic",
            "-chardev", "pty,id=char0",
            "-serial", "chardev:char0",
        ])
    else: # gui
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
        run_qemu(final_args)


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


def run_qemu(args):
    """Executes the QEMU command and handles interrupts."""
    print("--- Starting QEMU with the following command ---")
    print(subprocess.list2cmdline(args))
    print("-------------------------------------------------")
    try:
        process = subprocess.Popen(args)
        process.wait()
    except FileNotFoundError:
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)

    if 'process' in locals() and process.returncode != 0:
        print(f"\nQEMU exited with an error (code {process.returncode}).", file=sys.stderr)
        sys.exit(process.returncode)

def main():
    """Parses command-line arguments and launches the VM."""
    parser = argparse.ArgumentParser(
        description="Launch a QEMU AArch64 virtual machine with fully independent display and firmware options.",
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
        help="Specify firmware mode. 'uefi' (default) uses firmware to boot. 'bios' uses direct kernel boot."
    )
    parser.add_argument(
        "--console", choices=['gui', 'text'], default='gui',
        help="Choose the console type. 'gui' for a graphical window, 'text' for a serial console in the terminal."
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
        sys.exit(1)
    if config["cdrom"] and not os.path.exists(config["cdrom"]):
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
