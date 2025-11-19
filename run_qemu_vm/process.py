import asyncio
import re
import subprocess
import sys
import threading

from . import console


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


def run_qemu(args, config):
    """Executes the QEMU command and handles text console if needed."""
    print("--- Starting QEMU with the following command ---", flush=True)
    formatted_command = f"{args[0]} \\\n"
    formatted_command += " \\\n".join([f"    {subprocess.list2cmdline([arg])}" for arg in args[1:]])
    print(formatted_command, flush=True)
    print("-" * 50, flush=True)

    try:
        if config.get('console') == 'text':
            process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
            event, holder = threading.Event(), [None]
            thread = threading.Thread(target=parse_pty_device_from_thread, args=(process, event, holder))
            thread.daemon = True
            thread.start()
            if not event.wait(timeout=10.0) or not holder[0]:
                print("Error: Could not find PTY device in QEMU output within 10 seconds.", file=sys.stderr)
                process.kill()
                thread.join()
                sys.exit(1)

            # Hand off to the prompt_toolkit console manager
            try:
                return_code = asyncio.run(console.run_prompt_toolkit_console(process, holder[0], config['monitor_socket']))
            except KeyboardInterrupt:
                # This is a fallback; prompt_toolkit should handle Ctrl-C gracefully.
                print("\nInterrupted by user.", flush=True)
                return_code = 130
            finally:
                thread.join()

            sys.exit(return_code)
        else:
            process = subprocess.Popen(args)
            process.wait()
            if process.returncode != 0:
                sys.exit(process.returncode)
    except FileNotFoundError:
        print(f"Error: QEMU executable '{args[0]}' not found.", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nInterrupted")
        sys.exit(130)
