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

import sys
from pathlib import Path

# Ensure the package is in the path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from run_qemu_vm.main import main


if __name__ == "__main__":
    main()
