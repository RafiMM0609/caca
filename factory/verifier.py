import base64
import json
import os
import subprocess
from pathlib import Path

from .process import run
from .workspace import extract

DOCKER = ["docker", "--host=unix:///var/run/docker.sock"]


class Verifier:
    def verify(self, profile, source: Path, output: Path, job_id, cancelled=lambda: False):
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
        subprocess.run(DOCKER + ["rm", "-f", "sf-" + job_id], capture_output=True, timeout=20)

