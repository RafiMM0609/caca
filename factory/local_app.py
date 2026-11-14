"""Bounded local app lifecycle for trusted native development/test processes."""
import os
import shutil
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

    It sits **outside** the product's git working tree, as a sibling directory under
    ``data/product-data/<product>``. Living inside the repository was a defect: the
    controller commits a worktree with ``git add -A``, so a live SQLite file could be
    committed into the product history and would then be clobbered by the next checkout.
    A generated application may also legitimately own a ``data/`` directory of its own,
    so an inside-the-repo path collides with real source.

    It is still outside the verifier's disposable build tree, which is deleted after every
    verification, so data written by one run survives the next.
    """
    path = Path(settings.data) / "product-data" / project
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


def build_env(directory) -> dict:
    """Environment for running a profile's build commands.

    `local_env()` forwards only a small allowlist, which is deliberate: it must not leak factory
    or provider tokens into a build. But Zig resolves its global cache from `HOME` (or
    `XDG_CACHE_HOME`) and fails with `unable to resolve zig cache directory: AppDataDirUnavailable`
    when neither is set. That made `zig build` impossible in both the verifier and `factory run`,
    for the same reason, discovered twice. It lives here so the two callers cannot drift apart
    again: any new place that builds a product should use this rather than `local_env()` directly.

    The scratch home points inside `directory`, so a build cannot poison a shared cache and
    cannot read the operator's dotfiles.
    """
    env = local_env()
    home = Path(directory) / "home"
    env.setdefault("HOME", str(home))
    Path(env["HOME"]).mkdir(parents=True, exist_ok=True)
    return env


def zig_binary() -> str:
    """Path to the Zig compiler, or a bare name so `subprocess` reports the absence cleanly.

    `FACTORY_ZIG` is consulted first because the binary often lives outside the scheduler's PATH
    (a per-user install), and a profile that needs Zig must not depend on how the service was
    started. Falls back to PATH, then to the plain name.
    """
    configured = os.environ.get("FACTORY_ZIG", "")
    if configured:
        return configured
    return shutil.which("zig") or "zig"


def runtime_facts() -> str:
    """The real toolchain the verifier will build with, for worker instructions.

    A worker that is not told the interpreter version guesses it from training data. That is
    how a generated package.json came to claim `engines.node: ">=20.19.0"` while its own
    Nuxt dependency required `^22.22.3 || ^24.15.0` — a package that lies about what it
    supports. Reporting the measured versions lets the worker declare constraints that match
    the machine that will actually run `npm ci` and `node`.
    """
    facts = []
    # Zig is probed alongside Node because a worker building a Zig profile needs to know the
    # exact compiler version: Zig pre-1.0 breaks std API between minor releases, so a worker
    # guessing from training data writes code for the wrong version. An absent toolchain simply
    # produces no line, which is the honest answer -- the worker then knows it cannot use it.
    for label, argv in (("Node.js", ["node", "--version"]),
                        ("npm", ["npm", "--version"]),
                        ("Zig", [zig_binary(), "version"]),
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
        resolved = [arg.replace("{port}", str(self.port)) for arg in argv]
        # A relative executable is not looked up in `cwd` by subprocess: `Popen(['app'])` searches
        # PATH only. That is invisible for `node` (which is on PATH) and fatal for a compiled
        # binary served from the build output, where the argv is exactly `app`. Prefixing `./`
        # when the file exists in cwd is what makes a Zig binary runnable; when it does not
        # exist the argv is left untouched so a genuine failure reports the real path.
        if resolved and not os.path.isabs(resolved[0]) and "/" not in resolved[0]:
            candidate = Path(cwd) / resolved[0]
            if candidate.is_file() and os.access(candidate, os.X_OK):
                resolved[0] = "./" + resolved[0]
        command = [sys.executable, str(Path(__file__).with_name("watchdog.py")), str(os.getpid()),
                   *resolved]
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

