import asyncio
import queue
import re
import subprocess
import sys
import threading
import time

from . import console


def parse_pty_device_from_thread(process, event, result_holder, debug_info, output_queue):
    """Reads from process output in a thread, finds PTY device, and drains output."""
    debug_info['thread_start'] = time.time()
    pty_device_found = False
    for line in iter(process.stdout.readline, ''):
        # Always capture output for the Monitor view
        output_queue.put(line)

        # Only print to stdout if we haven't found the PTY yet (startup phase)
        if not pty_device_found:
            sys.stdout.write(line)
            sys.stdout.flush()
            match = re.search(r'char device redirected to (/dev/[^\s]+)', line)
            if match:
                result_holder[0] = match.group(1)
                debug_info['pty_found'] = time.time()
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
            debug_info = {'qemu_start': time.time()}
            # Create a thread-safe queue to capture QEMU stdout
            qemu_output_queue = queue.Queue()

            process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
            event, holder = threading.Event(), [None]
            thread = threading.Thread(target=parse_pty_device_from_thread, args=(process, event, holder, debug_info, qemu_output_queue))
            thread.daemon = True
            thread.start()
            if not event.wait(timeout=10.0) or not holder[0]:
                print("Error: Could not find PTY device in QEMU output within 10 seconds.", file=sys.stderr)
                process.kill()
                thread.join()
                sys.exit(1)

            debug_info['pty_ready'] = time.time()

            # Log timing information
            qemu_to_pty = debug_info['pty_found'] - debug_info['qemu_start']
            print(f"Info: PTY device detected {qemu_to_pty:.3f}s after QEMU start", flush=True)

            # Give QEMU a moment to fully initialize the PTY connection
            # This prevents the race condition where we open the PTY before QEMU connects
            time.sleep(0.05)
            debug_info['console_start'] = time.time()

            # Hand off to the prompt_toolkit console manager
            try:
                return_code = asyncio.run(console.run_prompt_toolkit_console(process, holder[0], config['monitor_socket'], qemu_output_queue, debug_info))
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
