import os
import selectors
import signal
import subprocess
import tempfile
import time


class Cancelled(RuntimeError):
    pass


class ProcessError(RuntimeError):
    """A child process failed. `output` carries what it printed; `reason` says why we stopped it.

    `reason` exists because a deadline and a crash were indistinguishable to the caller: the
    engine only saw "ProcessError" and printed "inspect task evidence", so an operator could not
    tell a worker that ran out of time from one that died on a non-zero exit. A repair worker
    hitting its deadline was reported as an unexplained failure for ten minutes of wall time.
    """

    def __init__(self, message, *, reason="exit", output=""):
        super().__init__(message)
        self.reason = reason
        self.output = output


def run(argv, *, cwd=None, env=None, input_data=b"", timeout=300, cancelled=lambda: False,
        max_output=4_000_000):
    """Bound output, wall time and descendants. Never interpolate shell strings."""
    with tempfile.TemporaryFile() as source:
        source.write(input_data)
        source.seek(0)
        process = subprocess.Popen(argv, cwd=cwd, env=env, stdin=source, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, start_new_session=True)
        output = bytearray()
        truncated = 0
        deadline = time.monotonic() + timeout
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        try:
            while selector.get_map():
                if cancelled():
                    raise Cancelled("Cancellation requested")
                if time.monotonic() > deadline:
                    stopped = ProcessError(
                        f"Process exceeded its {timeout}s deadline and was stopped", reason="timeout")
                    stopped.output = output.decode("utf-8", errors="replace")[-100000:]
                    raise stopped
                for key, _ in selector.select(timeout=0.2):
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                    else:
                        output.extend(chunk)
                        # A tool-using coding worker legitimately streams far more than this:
                        # `npm ci` and a Nuxt build emit megabytes of JSON events. Killing the
                        # worker for talking too much destroyed real work, so keep a bounded
                        # tail instead. Bounded memory is the requirement; the tail is what
                        # the reviewer, the transcript and error evidence actually read.
                        if len(output) > max_output:
                            excess = len(output) - max_output
                            del output[:excess]
                            truncated += excess
            code = process.wait(timeout=max(0.1, deadline - time.monotonic()))
            text = output.decode("utf-8", errors="replace")
            if truncated:
                text = f"[OUTPUT TRUNCATED: discarded {truncated} earlier characters]\n" + text
            if code:
                # Output can contain credentials; caller persists only explicitly reviewed data.
                raise ProcessError(f"Process failed with exit code {code}", reason="exit",
                                   output=text[-100000:])
            return text
        finally:
            selector.close()
            process.stdout.close()
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()

