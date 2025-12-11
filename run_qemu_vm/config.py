# --- Global Configuration & Executable Paths ---

# Path to the attribute log file, if enabled via command line.
ATTRIBUTE_LOG_FILE = None

# Path to the debug log file, if enabled via command line.
DEBUG_FILE = None

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
    "x86_64": "virtio",
    "i386": "virtio",
    "aarch64": "virtio",
    "riscv64": "virtio",
    "default": "pl011" # Fallback for other arches
}


# --- Network Configuration ---

# The network backend mode for QEMU user-mode networking (SLIRP/NAT).
NETWORK_MODE = "user"
NETWORK_ID = "net0"
NETWORK_PORT_FORWARDS = "hostfwd=tcp::2222-:22,hostfwd=tcp::6001-:6001"
NETWORK_BACKEND = f"{NETWORK_MODE},id={NETWORK_ID}" + (f",{NETWORK_PORT_FORWARDS}" if NETWORK_PORT_FORWARDS else "")
NETWORK_DEVICE = f"virtio-net-pci,netdev={NETWORK_ID}"

# --- Directory Sharing Configuration ---
VIRTFS_SECURITY_MODEL = "mapped-xattr"
MOUNT_TAG_PATTERN = r'^[a-zA-Z0-9_]+$'
MOUNT_TAG_ALLOWED_CHARS = "letters (a-z, A-Z), numbers (0-9), and underscores (_)"

# --- Text Console Mode Constants ---
MODE_SERIAL_CONSOLE = 'serial_console'
MODE_QEMU_MONITOR = 'qemu_monitor'
