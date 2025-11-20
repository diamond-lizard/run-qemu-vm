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


# --- UEFI Bootloader Helpers ---

def _get_uefi_bootloader_patterns(architecture):
    """Returns a list of UEFI bootloader filenames for a given architecture."""
    bootloader_patterns = {
        'aarch64': ['bootaa64.efi'],
        'x86_64': ['bootx64.efi'],
        'riscv64': ['bootriscv64.efi']
    }
    return bootloader_patterns.get(architecture)


def _find_uefi_bootloader_with_isoinfo(iso_path, patterns):
    """Tries to find a UEFI bootloader using isoinfo."""
    isoinfo_path = shutil.which("isoinfo")
    if not isoinfo_path:
        return None, None, "isoinfo not found"

    try:
        cmd = [isoinfo_path, '-i', iso_path, '-J', '-find', f'*/{patterns[0]}']
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        if result.stdout.strip():
            path_line = result.stdout.split('\n', 1)[0].strip()
            if path_line:
                p = Path(path_line)
                print(f"Info: Found UEFI bootloader: {p.name} at path {path_line} using isoinfo")
                return p.name, str(p), "isoinfo"
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        return None, None, f"isoinfo ({e})"
    return None, None, "isoinfo (no bootloader found)"


def _find_uefi_bootloader_with_7z(seven_zip_executable, iso_path, patterns):
    """Tries to find a UEFI bootloader using 7z."""
    try:
        result = subprocess.run([seven_zip_executable, 'l', iso_path], capture_output=True, text=True, check=True)
    except (FileNotFoundError, subprocess.CalledProcessError) as e:
        return None, None, f"7z ({e})"

    for line in result.stdout.splitlines():
        for pattern in patterns:
            if pattern in line.lower():
                parts = line.split()
                if len(parts) > 0 and parts[-1].lower().endswith(pattern):
                    bootloader_full_path = parts[-1]
                    p = Path(bootloader_full_path)
                    print(f"Info: Found UEFI bootloader: {p.name} at path {bootloader_full_path} using 7z")
                    return p.name, str(p).replace('/', '\\'), "7z"
    return None, None, "7z (no bootloader found)"


# --- Direct Kernel Boot Helpers ---

def _get_kernel_initrd_patterns():
    """Returns regex patterns for kernel and initrd files based on the host OS."""
    if platform.system() == 'Linux':
        kernel_patterns = [
            r'/vmlinuz$', r'/install(\.amd)?/.*vmlinuz', r'/boot/vmlinuz',
            r'vmlinuz$', r'boot/linux$'
        ]
        initrd_patterns = [
            r'/initrd\.gz$', r'/install(\.amd)?/.*initrd\.gz', r'/boot/initrd\.gz',
            r'/boot/initramfs\.gz', r'initrd\.gz$'
        ]
    else:
        # macOS/other patterns
        kernel_patterns = [r'vmlinuz', r'/boot/vmlinuz', r'boot/linux']
        initrd_patterns = [r'initrd', r'initramfs', r'/boot/initrd', r'/boot/initramfs']
    return kernel_patterns, initrd_patterns


class _IsoInfoParser:
    """Parses `isoinfo -l -R` output to find kernel and initrd candidates."""

    def __init__(self, kernel_patterns, initrd_patterns):
        self.kernel_patterns = kernel_patterns
        self.initrd_patterns = initrd_patterns
        self._candidate_kernels = []
        self._candidate_initrds = []

    def parse(self, listing_output):
        """Parses the full listing and returns kernel/initrd candidates."""
        for block in self._get_blocks(listing_output):
            self._process_block(block)
        return self._candidate_kernels, self._candidate_initrds

    def _get_blocks(self, listing_output):
        """Splits the isoinfo listing into directory blocks."""
        return [block.strip() for block in listing_output.split("\n\n") if block.strip()]

    def _process_block(self, block):
        """Processes a single directory block from the listing."""
        lines = block.splitlines()
        current_directory = self._parse_directory_from_header(lines)
        if not current_directory:
            return

        for line in lines:
            self._process_file_line(line, current_directory)

    def _parse_directory_from_header(self, block_lines):
        """Parses the directory path from the header of an isoinfo block."""
        dir_header = next((line for line in block_lines if line.startswith("Directory listing of")), None)
        if not dir_header:
            return None
        try:
            # e.g., "Directory listing of /isolinux':"
            return dir_header.split()[-1].strip("'").strip(":")
        except IndexError:
            if os.environ.get("DEBUG"):
                print(f"Debug: Could not parse directory from header: {dir_header}")
            return None

    def _process_file_line(self, line, current_directory):
        """Parses a file line and adds kernel/initrd candidates if found."""
        filename, file_size = self._parse_file_info_from_line(line)
        if not filename or not file_size or file_size < 100_000:
            return

        full_path = os.path.join('/', current_directory, filename).replace('\\', '/').replace('//', '/')

        self._add_candidate_if_match(full_path, file_size, self.kernel_patterns, 'kernel')
        self._add_candidate_if_match(full_path, file_size, self.initrd_patterns, 'initrd')

    def _parse_file_info_from_line(self, line):
        """
        Parses a file line from isoinfo output, returning filename and size.
        Returns (None, None) if the line is not a valid file entry.
        """
        if not line.startswith('-r-'):
            return None, None
        parts = line.split()
        if len(parts) < 10:
            return None, None
        try:
            filename = parts[-1]
            if filename in ('.', '..'):
                return None, None
            file_size = int(parts[4])
            return filename, file_size
        except (ValueError, IndexError) as e:
            if os.environ.get("DEBUG"):
                print(f"Debug: Skipping malformed ISO line: {line} ({e})")
            return None, None

    def _add_candidate_if_match(self, full_path, file_size, patterns, candidate_type):
        """Adds a candidate tuple if the path matches any of the given patterns."""
        lower_path = full_path.lower()
        for pattern in patterns:
            if re.search(pattern, lower_path):
                if os.environ.get("DEBUG"):
                    print(f"Debug: {candidate_type.capitalize()} candidate: {full_path} (size:{file_size})")

                if candidate_type == 'kernel':
                    self._candidate_kernels.append((full_path, file_size))
                elif candidate_type == 'initrd':
                    self._candidate_initrds.append((full_path, file_size))
                break


def _get_isoinfo_listing_output(isoinfo_path, iso_path):
    """Runs isoinfo to get a recursive directory listing of an ISO."""
    try:
        result = subprocess.run(
            [isoinfo_path, '-R', '-l', '-i', iso_path],
            capture_output=True, text=True, check=True, encoding='latin-1', errors='ignore'
        )
        return result.stdout, None
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        return None, f"isoinfo failed ({e})"


def _select_best_boot_candidates(candidate_kernels, candidate_initrds):
    """Sorts and selects the best kernel and initrd candidates."""
    if not candidate_kernels or not candidate_initrds:
        return None, None

    # Sort by path depth (shallower is better) and then by size (larger is better)
    candidate_kernels.sort(key=lambda x: (x[0].count('/'), -x[1]))
    candidate_initrds.sort(key=lambda x: (x[0].count('/'), -x[1]))

    best_kernel = candidate_kernels[0][0]
    best_initrd = candidate_initrds[0][0]

    print(f"Info: Best kernel candidate: {best_kernel}")
    print(f"Info: Best initrd candidate: {best_initrd}")

    return best_kernel, best_initrd


def _find_kernel_initrd_with_isoinfo(iso_path, kernel_patterns, initrd_patterns):
    """Tries to find kernel and initrd in an ISO using isoinfo."""
    isoinfo_path = shutil.which("isoinfo")
    if not isoinfo_path:
        return None, None, "isoinfo not found"

    listing_output, error = _get_isoinfo_listing_output(isoinfo_path, iso_path)
    if error:
        return None, None, error

    parser = _IsoInfoParser(kernel_patterns, initrd_patterns)
    candidate_kernels, candidate_initrds = parser.parse(listing_output)

    found_kernel, found_initrd = _select_best_boot_candidates(candidate_kernels, candidate_initrds)

    if found_kernel and found_initrd:
        return found_kernel, found_initrd, "isoinfo"
    else:
        return None, None, "isoinfo (candidates not found)"


def _get_7z_listing(seven_zip_executable, iso_path):
    """Runs 7z to get a file listing of an ISO."""
    try:
        result = subprocess.run([seven_zip_executable, 'l', iso_path], capture_output=True, text=True, check=True)
        return result.stdout, None
    except (FileNotFoundError, subprocess.CalledProcessError) as e:
        return None, f"7z failed ({e})"


def _is_7z_kernel_candidate(path, patterns):
    """Checks if a path from 7z output is a likely kernel candidate."""
    lower_path = path.lower()
    for pattern in patterns:
        if pattern in lower_path and not lower_path.endswith('.mod') and lower_path.endswith(('bin', 'z', 'bimage', 'elf')):
            return True
    return False


def _is_7z_initrd_candidate(path, patterns):
    """Checks if a path from 7z output is a likely initrd candidate."""
    lower_path = path.lower()
    for pattern in patterns:
        if pattern in lower_path and lower_path.endswith(('.img', '.gz')):
            return True
    return False


def _parse_7z_listing_for_boot_files(listing_output, kernel_patterns, initrd_patterns):
    """Parses 7z listing output to find the first kernel and initrd candidates."""
    found_kernel, found_initrd = None, None
    for line in listing_output.splitlines():
        parts = line.split()
        if len(parts) > 0:
            full_path = parts[-1].lstrip('./').replace('\\', '/')

            if not found_kernel and _is_7z_kernel_candidate(full_path, kernel_patterns):
                found_kernel = full_path

            if not found_initrd and _is_7z_initrd_candidate(full_path, initrd_patterns):
                found_initrd = full_path

            if found_kernel and found_initrd:
                break  # Found both, no need to search further

    return found_kernel, found_initrd


def _find_kernel_initrd_with_7z(seven_zip_executable, iso_path, kernel_patterns, initrd_patterns):
    """Tries to find kernel and initrd in an ISO using 7z."""
    listing_output, error = _get_7z_listing(seven_zip_executable, iso_path)
    if error:
        return None, None, error

    found_kernel, found_initrd = _parse_7z_listing_for_boot_files(
        listing_output, kernel_patterns, initrd_patterns
    )

    if found_kernel and found_initrd:
        print(f"Info: Found Kernel: {found_kernel} (using 7z)")
        print(f"Info: Found Initrd: {found_initrd} (using 7z)")
        return found_kernel, found_initrd, "7z"

    return None, None, "7z (not found)"


def _get_direct_boot_kernel_cmdline(config):
    """Determines the appropriate kernel 'console=' argument based on serial device."""
    serial_profile_key = config.get("serial_device") or app_config.ARCH_DEFAULT_SERIAL.get(config['architecture'], app_config.ARCH_DEFAULT_SERIAL['default'])
    if serial_profile_key == 'virtio':
        return "console=hvc0 panic=1"
    return "console=ttyS0,115200n8 panic=1"


def _execute_7z_extraction(seven_zip_executable, iso_path, temp_dir, extract_path):
    """Executes the 7z command to extract a file from an ISO."""
    return subprocess.run(
        [seven_zip_executable, "e", iso_path, f"-o{temp_dir}", extract_path],
        capture_output=True, text=True, check=False
    )


def _handle_7z_extraction_result(result, output_path, extract_path):
    """Checks the result of a 7z extraction and raises an error on failure."""
    if result.returncode != 0 or not os.path.exists(output_path):
        print(f"Error: Failed to extract '{extract_path}' from ISO.", file=sys.stderr)
        if os.environ.get("DEBUG"):
            print(f"Debug: 7z output: {result.stdout}\nDebug: 7z errors: {result.stderr}", file=sys.stderr)
        raise RuntimeError(f"Failed to extract {extract_path}")


def _extract_single_file_from_iso(seven_zip_executable, iso_path, file_in_iso, temp_dir):
    """Extracts a single file from an ISO into a temporary directory."""
    base_name = os.path.basename(file_in_iso)
    output_path = os.path.join(temp_dir, base_name)
    extract_path = file_in_iso.lstrip('/')

    if os.environ.get("DEBUG"):
        print(f"Debug: Extracting '{extract_path}' from ISO to '{output_path}'")

    result = _execute_7z_extraction(seven_zip_executable, iso_path, temp_dir, extract_path)
    _handle_7z_extraction_result(result, output_path, extract_path)

    print(f"Info: Extracted {base_name} to: {output_path}")
    return output_path


def _extract_iso_files(seven_zip_executable, iso_path, files_to_extract, temp_dir):
    """Extracts specified files from an ISO into a temporary directory."""
    for file_in_iso in files_to_extract:
        output_path = _extract_single_file_from_iso(
            seven_zip_executable, iso_path, file_in_iso, temp_dir
        )
        yield file_in_iso, output_path


def _run_direct_kernel_boot(args, config, kernel_cmd_line):
    """Adds final arguments for direct kernel boot and executes QEMU."""
    print(f"Info: Direct Kernel Boot with append args: '{kernel_cmd_line}'")

    monitor_socket = f"/tmp/qemu-monitor-{os.getpid()}.sock"
    config['monitor_socket'] = monitor_socket
    args.extend(["-monitor", f"unix:{monitor_socket},server,nowait", "-chardev", "pty,id=char0"])

    serial_profile_key = config.get("serial_device") or app_config.ARCH_DEFAULT_SERIAL.get(config['architecture'], app_config.ARCH_DEFAULT_SERIAL['default'])
    serial_args = app_config.SERIAL_DEVICE_PROFILES[serial_profile_key]['args']
    print(f"Info: Using '{serial_profile_key}' serial profile for Direct Kernel Boot.")
    args.extend(serial_args)

    process.run_qemu(args, config)
    sys.exit(0)


def _create_uefi_startup_script(temp_dir, bootloader_script_path):
    """Creates a startup.nsh file in the given directory to automate UEFI boot."""
    startup_script_path = Path(temp_dir) / "startup.nsh"
    script_content = (
        f"# Auto-generated by run-qemu-vm.py\n"
        f"echo -off\n"
        f"echo 'Attempting to boot from CD-ROM...'\n"
        f"FS0:\n"
        f"{bootloader_script_path}\n"
    )
    with open(startup_script_path, "w") as f:
        f.write(script_content)
    print(f"Info: Created temporary startup.nsh in '{temp_dir}'")


def _handle_uefi_bootloader_not_found(tools_tried, patterns):
    """Handles the case where no UEFI bootloader could be found, exiting or warning as appropriate."""
    is_macos = platform.system() == 'Darwin'
    if not is_macos:
        print(f"Error: Could not find UEFI bootloader. Tried: {', '.join(tools_tried)}", file=sys.stderr)
        print("       Install either genisoimage/isoinfo or p7zip-full/7z.", file=sys.stderr)
        sys.exit(1)
    else:
        print(f"Warning: Could not find a suitable bootloader ({', '.join(patterns)}) in the ISO. Tried: {', '.join(tools_tried)}", file=sys.stderr)
        return None, None


def find_uefi_bootloader(seven_zip_executable, iso_path, architecture):
    """Inspects an ISO to find the UEFI bootloader file for the given architecture."""
    patterns = _get_uefi_bootloader_patterns(architecture)
    if not patterns:
        return None, None
    print(f"Info: Searching for UEFI bootloader in '{iso_path}' for {architecture}...")

    finders = []
    is_macos = platform.system() == 'Darwin'

    # Prefer isoinfo on Linux
    if not is_macos:
        finders.append(lambda: _find_uefi_bootloader_with_isoinfo(iso_path, patterns))

    # Fallback to 7z if available
    finders.append(lambda: _find_uefi_bootloader_with_7z(seven_zip_executable, iso_path, patterns))

    name, path, reasons = _run_uefi_bootloader_finders(finders)

    if name:
        return name, path

    # If we get here, no tool worked
    return _handle_uefi_bootloader_not_found(reasons, patterns)


def _try_isoinfo_for_kernel_initrd(iso_path, kernel_patterns, initrd_patterns):
    """
    Attempts to find kernel and initrd using isoinfo.
    Returns (kernel, initrd, reason) on success, or (None, None, reason) on failure.
    The reason is for logging.
    """
    kernel, initrd, reason = _find_kernel_initrd_with_isoinfo(iso_path, kernel_patterns, initrd_patterns)
    if kernel and initrd:
        # isoinfo provides full paths, which is what we need for extraction on Linux.
        # For other platforms, we historically stripped the leading slash.
        if platform.system() == 'Linux':
            return kernel, initrd, reason
        return kernel.lstrip('/'), initrd.lstrip('/'), reason
    return None, None, reason


def _try_7z_for_kernel_initrd(seven_zip_executable, iso_path, kernel_patterns, initrd_patterns):
    """
    Attempts to find kernel and initrd using 7z.
    Returns (kernel, initrd, reason) on success, or (None, None, reason) on failure.
    The reason is for logging.
    """
    kernel, initrd, reason = _find_kernel_initrd_with_7z(seven_zip_executable, iso_path, kernel_patterns, initrd_patterns)
    if kernel and initrd:
        return kernel.lstrip('/'), initrd.lstrip('/'), reason
    return None, None, reason


def _warn_kernel_initrd_not_found(tools_tried):
    """Prints a warning that kernel/initrd could not be found."""
    print(f"Warning: Could not find a suitable Linux kernel and initial ramdisk (initrd) in the ISO for direct boot. Tried: {', '.join(tools_tried)}", file=sys.stderr)
    print("Warning: Falling back to standard BIOS boot (direct kernel boot disabled).", file=sys.stderr)


def _run_uefi_bootloader_finders(finders):
    """
    Iterates through UEFI bootloader finder functions until one succeeds.

    Each finder should return a tuple of (name, path, reason).
    Returns a tuple of (name, path) on the first success.
    If all finders fail, returns (None, None) and a list of failure reasons.
    """
    failure_reasons = []
    for finder in finders:
        name, path, reason = finder()
        if name:
            return name, path, []
        if reason:
            failure_reasons.append(reason)
    return None, None, failure_reasons


def _run_kernel_initrd_finders(finders):
    """
    Iterates through finder functions until one succeeds.

    Each finder should return a tuple of (kernel, initrd, reason).
    Returns a tuple of (kernel, initrd) on the first success.
    If all finders fail, returns (None, None) and a list of failure reasons.
    """
    failure_reasons = []
    for finder in finders:
        kernel, initrd, reason = finder()
        if kernel and initrd:
            return kernel, initrd, []
        if reason:
            failure_reasons.append(reason)
    return None, None, failure_reasons


def find_kernel_and_initrd(seven_zip_executable, iso_path):
    """
    Inspects an ISO to find paths to a Linux kernel and initial ramdisk.
    It tries a sequence of tools ('isoinfo', then '7z').
    """
    print(f"Info: Searching for kernel and initrd in '{iso_path}'...")
    kernel_patterns, initrd_patterns = _get_kernel_initrd_patterns()

    finders = [
        lambda: _try_isoinfo_for_kernel_initrd(iso_path, kernel_patterns, initrd_patterns),
        lambda: _try_7z_for_kernel_initrd(seven_zip_executable, iso_path, kernel_patterns, initrd_patterns),
    ]

    kernel, initrd, reasons = _run_kernel_initrd_finders(finders)

    if kernel and initrd:
        return kernel, initrd

    _warn_kernel_initrd_not_found(reasons)
    return None, None



def _extract_kernel_and_initrd(config, kernel_file, initrd_file, temp_dir):
    """Extracts kernel and initrd from ISO to a temporary directory, exiting on failure."""
    try:
        extracted_files = dict(_extract_iso_files(
            config["seven_zip_executable"], config["cdrom"],
            [kernel_file, initrd_file], temp_dir
        ))
        kernel_path = extracted_files[kernel_file]
        initrd_path = extracted_files[initrd_file]
        return kernel_path, initrd_path
    except (RuntimeError, KeyError) as e:
        print(f"Error: Failed during file extraction from ISO: {e}", file=sys.stderr)
        sys.exit(1)


def _run_direct_boot_linux(args, config, kernel_file, initrd_file, kernel_cmd_line):
    """Handles direct kernel boot on Linux by extracting files from the ISO."""
    with tempfile.TemporaryDirectory(prefix="qemu-kernel-") as temp_dir:
        kernel_path, initrd_path = _extract_kernel_and_initrd(
            config, kernel_file, initrd_file, temp_dir
        )

        args.extend(["-kernel", kernel_path, "-initrd", initrd_path])
        args.extend(["-append", kernel_cmd_line, "-nographic"])
        _run_direct_kernel_boot(args, config, kernel_cmd_line)


def _run_direct_boot_iso_syntax(args, config, kernel_file, initrd_file, kernel_cmd_line):
    """Handles direct kernel boot on non-Linux systems using QEMU's 'iso:' syntax."""
    iso_kernel_path = kernel_file.lstrip('/')
    iso_initrd_path = initrd_file.lstrip('/')
    args.extend([
        "-kernel", f"iso:{config['cdrom']}:{iso_kernel_path}",
        "-initrd", f"iso:{config['cdrom']}:{iso_initrd_path}",
        "-append", kernel_cmd_line,
        "-nographic"
    ])
    _run_direct_kernel_boot(args, config, kernel_cmd_line)


def add_direct_kernel_boot_args(base_args, config, kernel_file, initrd_file):
    """
    Constructs QEMU arguments for direct kernel boot and executes QEMU.
    This function is a terminal operation; it will exit the script.
    """
    args = list(base_args)  # Keep the original arguments including -cdrom
    kernel_cmd_line = _get_direct_boot_kernel_cmdline(config)

    # Switch boot order to hard disk (c) so CD-ROM (d) is only used for bootstrapping
    if any(arg.startswith('order=') for arg in args if isinstance(arg, str)):
        for i, arg in enumerate(args):
            if isinstance(arg, str) and arg.startswith('order='):
                args[i] = 'order=c'  # Change boot order to hard disk
                break
    else:
        args.extend(["-boot", "order=c"])

    if platform.system() == 'Linux':
        _run_direct_boot_linux(args, config, kernel_file, initrd_file, kernel_cmd_line)
    else:
        _run_direct_boot_iso_syntax(args, config, kernel_file, initrd_file, kernel_cmd_line)


def _run_uefi_with_automation(base_args, config, bootloader_script_path):
    """Runs QEMU with a temporary startup.nsh to automate UEFI boot."""
    with tempfile.TemporaryDirectory(prefix="qemu-uefi-boot-") as temp_dir:
        _create_uefi_startup_script(temp_dir, bootloader_script_path)
        automated_args = list(base_args)
        automated_args.extend([
            "-drive", f"if=none,id=boot-script,format=raw,file=fat:rw:{temp_dir}",
            "-device", "usb-storage,drive=boot-script"
        ])
        process.run_qemu(automated_args, config)


def create_and_run_uefi_with_automation(base_args, config):
    """Creates a temporary startup.nsh to automate UEFI boot and runs QEMU."""
    bootloader_name, bootloader_script_path = find_uefi_bootloader(config['seven_zip_executable'], config['cdrom'], config['architecture'])
    if bootloader_name and bootloader_script_path:
        _run_uefi_with_automation(base_args, config, bootloader_script_path)
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
