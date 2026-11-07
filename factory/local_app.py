"""Bounded local app lifecycle for trusted native development/test processes."""
import os
import signal
import socket
import subprocess
import threading
import time
import sys
from pathlib import Path

import httpx

from .process import Cancelled, ProcessError


def local_env():
    # Keep network trust/proxy configuration for installs, never provider or factory tokens.
    keys = ("PATH", "LANG", "SSL_CERT_FILE", "NODE_EXTRA_CA_CERTS", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY")
    return {key: os.environ[key] for key in keys if key in os.environ}


class LocalApp:
    def __init__(self, argv, cwd, env=None):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}"
        child_env = local_env() if env is None else dict(env)
        child_env.update({"PORT": str(self.port), "NITRO_PORT": str(self.port), "HOST": "127.0.0.1",
                          "NITRO_HOST": "127.0.0.1", "NUXT_TELEMETRY_DISABLED": "1"})
        command = [sys.executable, str(Path(__file__).with_name("watchdog.py")), str(os.getpid()),
                   *[arg.replace("{port}", str(self.port)) for arg in argv]]
        self.process = subprocess.Popen(command, cwd=cwd,
                                        env=child_env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, start_new_session=True)
        self.output = bytearray()
        self.overflow = threading.Event()
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        for chunk in iter(lambda: self.process.stdout.read(4096), b""):
            if len(self.output) + len(chunk) > 2_000_000:
                self.overflow.set()
                self.kill()
                break
            self.output.extend(chunk)

    def kill(self):
        try:
            os.killpg(self.process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def wait_ready(self, path="/", timeout=120, cancelled=lambda: False):
        deadline = time.monotonic() + timeout
        with httpx.Client(trust_env=False, timeout=2, follow_redirects=False) as client:
            while time.monotonic() < deadline:
                if cancelled():
                    raise Cancelled("Local application cancelled")
                if self.process.poll() is not None or self.overflow.is_set():
                    raise ProcessError("Local application exited before readiness")
                try:
                    if client.get(self.url + path).status_code == 200:
                        return self.url
                except httpx.HTTPError:
                    pass
                time.sleep(0.2)
        raise ProcessError("Local application readiness timed out")

    def close(self):
        self.kill()
        self.process.wait(timeout=10)
        self.reader.join(timeout=5)
        self.process.stdout.close()

