#!/usr/bin/env python3
"""
This script serves as the executable entry point for the run-qemu-vm application.

Its sole purpose is to configure the Python path to include the project's root
directory, allowing the `run_qemu_vm` package to be imported, and then to
execute the main function from the `run_qemu_vm.main` module.
"""

import sys
from pathlib import Path

# Ensure the package is in the path by adding the project root directory.
# The script is in `bin/`, so the project root is two levels up.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from run_qemu_vm.main import main

if __name__ == "__main__":
    main()
