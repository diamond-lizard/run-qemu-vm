import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from . import config as app_config
from . import process


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

            process.run_qemu(args, config)
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

        process.run_qemu(args, config)
        sys.exit(0)


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
            process.run_qemu(automated_args, config)
    else:
        print("Warning: Proceeding without boot automation script.", file=sys.stderr)
        process.run_qemu(base_args, config)


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
