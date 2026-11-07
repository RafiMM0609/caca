import base64
import json
import os
import subprocess
import shutil
import tempfile
import time
import sys
from pathlib import Path

from .process import run
from .workspace import extract, clone_tree, files_under

DOCKER = ["docker", "--host=unix:///var/run/docker.sock"]


class Verifier:
    def verify(self, profile, source: Path, output: Path, job_id, cancelled=lambda: False):
        if profile.runner == "native":
            return NativeVerifier().verify(profile, source, output, job_id, cancelled)
        checks = Path(profile.checks_dir).resolve()
        if not checks.is_dir():
            raise ValueError("Operator-owned checks directory is missing")
        output.mkdir()
        # Copy to an expendable container workspace; the approved source stays read-only.
        name = "sf-" + job_id
        script = Path(__file__).with_name("sandbox_runner.py").read_text()
        argv = DOCKER + ["run", "--rm", "--init", "--name", name,
                        "--label", "software-factory.job=" + job_id,
                        "--pull=never", "--network=none", "--read-only", "--cap-drop=ALL",
                        "--security-opt=no-new-privileges", "--pids-limit=128",
                        "--memory=" + str(profile.memory_mb) + "m", "--cpus=" + str(profile.cpus),
                        "--user", f"{os.getuid()}:{os.getgid()}",
                        "--tmpfs", "/tmp:rw,nosuid,nodev,size=128m,mode=1777",
                        "--tmpfs", "/work:rw,nosuid,nodev,size=512m,mode=1777",
                        "--tmpfs", "/output:rw,nosuid,nodev,size=64m,mode=1777",
                        "--mount", f"type=bind,src={source.resolve()},dst=/source,readonly",
                        "--mount", f"type=bind,src={checks},dst=/checks,readonly",
                        "--env", "FACTORY_OUTPUT=/output", "--env", "PYTHONDONTWRITEBYTECODE=1",
                        "--entrypoint", "python3", profile.image, "-c", script, *profile.verify_argv]
        try:
            response = json.loads(run(argv, timeout=profile.timeout, cancelled=cancelled, max_output=32_000_000))
            extract(base64.b64decode(response["artifact"], validate=True), output)
            return str(response["log"])[-100000:]
        finally:
            self.cleanup(job_id)

    @staticmethod
    def cleanup(job_id):
        if not shutil.which("docker"):
            return
        subprocess.run(DOCKER + ["rm", "-f", "sf-" + job_id], capture_output=True, timeout=20)



class NativeVerifier:
    """Opt-in trusted local execution. NOT a filesystem/network/resource sandbox."""
    def verify(self, profile, source, output, job_id, cancelled=lambda: False):
        if sys.platform != "linux":
            raise RuntimeError("Native execution currently validated for Linux only")
        if os.environ.get("FACTORY_ALLOW_NATIVE") != "1":
            raise RuntimeError("Native execution requires FACTORY_ALLOW_NATIVE=1")
        deadline = time.monotonic() + profile.timeout
        logs = []
        with tempfile.TemporaryDirectory(prefix="sf-native-", dir=source.parent) as directory:
            work = Path(directory) / "app"
            clone_tree(source, work)
            env = {key: os.environ[key] for key in ("PATH", "LANG", "SSL_CERT_FILE", "NODE_EXTRA_CA_CERTS") if key in os.environ}
            env.update({"CI": "true", "npm_config_cache": str(Path(directory) / "npm-cache")})
            for index, command in enumerate(profile.commands):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError("Native verification deadline exceeded")
                log = run(command, cwd=work, env=env, timeout=remaining, cancelled=cancelled, max_output=2_000_000)
                logs.append(f"STEP {index + 1} PASS: {command[0]}\n{log}")
            built = work / profile.artifact_dir
            if not built.is_dir() or built.is_symlink():
                raise ValueError("Missing artifact directory")
            list(files_under(built, limit=20_000_000))
            shutil.copytree(built, output)
        return "\n".join(logs)[-100000:]
