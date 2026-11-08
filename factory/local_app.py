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


def app_data_dir(settings, project: str) -> Path:
    """The persistent data directory for one product.

    It lives beside the product's git repository, never inside it: a SQLite file committed
    to the repository would be overwritten by the next checkout, and it would be shipped in
    the artifact. It also sits outside the verifier's disposable build tree, which is deleted
    after every verification, so data written by one run survives the next.
    """
    path = settings.data / "products" / project / "data"
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def expand_env(env: dict[str, str], *, data_dir: Path | None = None, app_dir: Path | None = None) -> dict[str, str]:
    """Substitute the supported placeholders in an operator-authored profile env."""
    expanded = {}
    for name, value in env.items():
        if data_dir is not None:
            value = value.replace("{data}", str(data_dir))
        if app_dir is not None:
            value = value.replace("{app}", str(app_dir))
        expanded[name] = value
    return expanded


def runtime_facts() -> str:
    """The real toolchain the verifier will build with, for worker instructions.

    A worker that is not told the interpreter version guesses it from training data. That is
    how a generated package.json came to claim `engines.node: ">=20.19.0"` while its own
    Nuxt dependency required `^22.22.3 || ^24.15.0` — a package that lies about what it
    supports. Reporting the measured versions lets the worker declare constraints that match
    the machine that will actually run `npm ci` and `node`.
    """
    facts = []
    for label, argv in (("Node.js", ["node", "--version"]),
                        ("npm", ["npm", "--version"]),
                        ("Python", [sys.executable, "--version"])):
        try:
            out = subprocess.run(argv, capture_output=True, text=True, timeout=15,
                                 env=local_env()).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            out = ""
        if out:
            facts.append(f"- {label}: {out.splitlines()[0]}")
    return "\n".join(facts)


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

