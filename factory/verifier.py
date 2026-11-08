import base64
import json
import os
import subprocess
import shutil
import tempfile
import time
import sys
from pathlib import Path

from .process import run, ProcessError
from .workspace import extract, clone_tree, files_under

DOCKER = ["docker", "--host=unix:///var/run/docker.sock"]

# A relative checks_dir is anchored to the checkout containing this package, never to the caller's
# working directory. Operators start the API and a scheduler from different shells; a CWD-relative
# checks directory would resolve somewhere else (or vanish), which is exactly the failure mode where
# a profile looks validated while running no operator checks.
REPO_ROOT = Path(__file__).resolve().parents[1]


def resolve_checks_dir(profile) -> Path:
    configured = Path(profile.checks_dir)
    return configured.resolve() if configured.is_absolute() else (REPO_ROOT / configured).resolve()


class Verifier:
    def verify(self, profile, source: Path, output: Path, job_id, cancelled=lambda: False, **testing):
        if profile.runner == "native":
            return NativeVerifier().verify(profile, source, output, job_id, cancelled, **testing)
        checks = resolve_checks_dir(profile)
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
                        # The container has no persistent mount, so a profile env reaches the
                        # build's configuration only. Persistence is a native-runner feature;
                        # do not describe the Docker profile as stateful.
                        *[arg for name, value in profile.env.items() for arg in ("--env", f"{name}={value}")],
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
    """Opt-in trusted local execution. NOT a filesystem/network/sandbox."""

    @staticmethod
    def _declared_node_engine(work: Path) -> str:
        """The `engines.node` a generated package.json claims, if any.

        npm treats `engines` as advisory, so a package can build locally while declaring a
        range that excludes the machine it runs on. Recording the claim next to the measured
        runtime makes that mismatch visible in verification evidence instead of invisible.
        """
        manifest = work / "package.json"
        if not manifest.is_file():
            return ""
        try:
            declared = json.loads(manifest.read_text()).get("engines", {}).get("node", "")
        except (ValueError, OSError, AttributeError):
            return ""
        return declared if isinstance(declared, str) else ""

    def verify(self, profile, source, output, job_id, cancelled=lambda: False, *, suite=None, evidence=None,
               on_event=lambda *_: None):
        if sys.platform != "linux":
            raise RuntimeError("Native execution currently validated for Linux only")
        if os.environ.get("FACTORY_ALLOW_NATIVE") != "1":
            raise RuntimeError("Native execution requires FACTORY_ALLOW_NATIVE=1")
        deadline = time.monotonic() + profile.timeout
        logs = []
        with tempfile.TemporaryDirectory(prefix="sf-native-", dir=source.parent) as directory:
            work = Path(directory) / "app"
            clone_tree(source, work)
            from .local_app import expand_env, local_env
            env = local_env()
            env.update({"CI": "true", "npm_config_cache": os.environ.get("FACTORY_NPM_CACHE", str(Path(directory) / "npm-cache"))})
            # Verification gets its own throwaway data directory, never the product's live one.
            # Tests that create and then list records must behave identically on every run, so
            # they must not see rows left behind by the previous build.
            scratch_data = Path(directory) / "data"
            scratch_data.mkdir(parents=True, exist_ok=True)
            if profile.env:
                env.update(expand_env(profile.env, data_dir=scratch_data, app_dir=work))
                logs.append("ENV from profile: " + ", ".join(sorted(profile.env)))
            declared = self._declared_node_engine(work)
            if declared:
                logs.append(f"ENGINE declared by package.json: node {declared}")
            for index, command in enumerate(profile.commands):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError("Native verification deadline exceeded")
                try:
                    log = run(command, cwd=work, env=env, timeout=remaining, cancelled=cancelled, max_output=2_000_000)
                except ProcessError as exc:
                    logs.append(f"STEP {index + 1} FAILED: {command[0]}\n" + getattr(exc, "output", str(exc)))
                    (source.parent / "verification.txt").write_text("\n".join(logs)[-100000:])
                    raise
                logs.append(f"STEP {index + 1} PASS: {command[0]}\n{log}")
            if profile.checks_dir:
                # Operator-owned checks run in the same disposable build tree, after the build,
                # so a passing build cannot skip them. They are not generated application code.
                checks = resolve_checks_dir(profile)
                if not checks.is_dir():
                    raise ValueError("Operator-owned checks directory is missing")
                for check_index, check in enumerate(profile.checks_argv):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise RuntimeError("Native verification deadline exceeded")
                    argv = [str(arg).replace("{checks}", str(checks)) for arg in check]
                    try:
                        log = run(argv, cwd=work, env=env, timeout=remaining,
                                  cancelled=cancelled, max_output=2_000_000)
                    except ProcessError as exc:
                        logs.append(f"CHECK {check_index + 1} FAILED: {check[0]}\n" + getattr(exc, "output", str(exc)))
                        (source.parent / "verification.txt").write_text("\n".join(logs)[-100000:])
                        raise
                    logs.append(f"CHECK {check_index + 1} PASS: {check[0]}\n{log}")
            if suite:
                from .local_app import LocalApp
                from .testing import run_suite
                on_event("tester.waiting_app", {})
                app = LocalApp(profile.serve_argv, work, env)
                try:
                    url = app.wait_ready(profile.readiness_path, timeout=60, cancelled=cancelled)
                    on_event("tester.running", {"cases": len(suite.cases)})
                    report = run_suite(suite, url, evidence, profile.qa_timeout, cancelled)
                    on_event("tester.passed", {"cases": len(report["cases"])})
                except BaseException:
                    on_event("tester.failed", {"stage": "execution"})
                    raise
                finally:
                    app.close()
            built = work / profile.artifact_dir
            if not built.is_dir() or built.is_symlink():
                raise ValueError("Missing artifact directory")
            list(files_under(built, limit=20_000_000))
            shutil.copytree(built, output)
        return "\n".join(logs)[-100000:]
