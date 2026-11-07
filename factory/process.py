import os
import selectors
import signal
import subprocess
import tempfile
import time


class Cancelled(RuntimeError):
    pass


class ProcessError(RuntimeError):
    pass


def run(argv, *, cwd=None, env=None, input_data=b"", timeout=300, cancelled=lambda: False,
        max_output=4_000_000):
    """Bound output, wall time and descendants. Never interpolate shell strings."""
    with tempfile.TemporaryFile() as source:
        source.write(input_data)
        source.seek(0)
        process = subprocess.Popen(argv, cwd=cwd, env=env, stdin=source, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, start_new_session=True)
        output = bytearray()
        deadline = time.monotonic() + timeout
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        try:
            while selector.get_map():
                if cancelled():
                    raise Cancelled("Cancellation requested")
                if time.monotonic() > deadline:
                    raise ProcessError(f"Process exceeded {timeout}s deadline")
                for key, _ in selector.select(timeout=0.2):
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                    else:
                        output.extend(chunk)
                        if len(output) > max_output:
                            raise ProcessError("Process output exceeded limit")
            code = process.wait(timeout=max(0.1, deadline - time.monotonic()))
            text = output.decode("utf-8", errors="replace")
            if code:
                # Output can contain credentials; caller persists only explicitly reviewed data.
                error = ProcessError(f"Process failed with exit code {code}")
                error.output = text[-100000:]
                raise error
            return text
        finally:
            selector.close()
            process.stdout.close()
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()

