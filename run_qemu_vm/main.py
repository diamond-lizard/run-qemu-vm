import argparse
import os
import platform
import re
import shutil
import sys
from pathlib import Path

from . import boot, config as app_config, process


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
        kernel_file, initrd_file = boot.find_kernel_and_initrd(config["seven_zip_executable"], config["cdrom"])

        if kernel_file and initrd_file:
            boot.add_direct_kernel_boot_args(args, config, kernel_file, initrd_file)
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
        boot.create_and_run_uefi_with_automation(args, config)
    else:
        process.run_qemu(args, config)


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
            qemu_prefix = boot.get_qemu_prefix(config['brew_executable'])
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

        boot.prepare_uefi_vars_file(config["uefi_vars_path"], config["uefi_code_path"])

    config['uefi_code'] = config.pop('uefi_code_path', None)
    config['uefi_vars'] = config.pop('uefi_vars_path', None)

    build_qemu_args(config)
