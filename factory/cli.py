import argparse
import json
import logging
import os
import secrets
import shutil
import subprocess
from pathlib import Path

import uvicorn

from .config import Settings
from .db import Store
from .engine import Engine


def main():
    parser = argparse.ArgumentParser(prog="factory")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="Create local secret file; never overwrites existing secrets")
    init.add_argument("--data", default="data")
    api = commands.add_parser("api")
    api.add_argument("--host", default="127.0.0.1")
    api.add_argument("--port", type=int, default=8080)
    scheduler = commands.add_parser("scheduler")
    scheduler.add_argument("--lane", choices=["build", "deploy"], default="build")
    run_p = commands.add_parser("run", help="Serve a project's published artifact with its persistent data directory")
    run_p.add_argument("project")
    run_p.add_argument("--port", type=int, default=3000)
    backup = commands.add_parser("backup")
    backup.add_argument("destination")
    commands.add_parser("doctor")
    reconcile = commands.add_parser("reconcile")
    reconcile.add_argument("deployment")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    os.umask(0o077)
    if args.command == "init":
        directory = Path(args.data).resolve()
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = directory / "secrets.env"
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as file:
            file.write("FACTORY_DATA=" + str(directory) + "\n")
            file.write("FACTORY_OPERATOR_TOKEN=" + secrets.token_urlsafe(32) + "\n")
            file.write("FACTORY_RELEASE_TOKEN=" + secrets.token_urlsafe(32) + "\n")
        print("Created " + str(path) + "; load it locally or via systemd EnvironmentFile. Do not commit it.")
        return
    settings = Settings.from_env()
    settings.validate()
    if args.command == "api":
        uvicorn.run("factory.api:create_app", factory=True, host=args.host, port=args.port,
                    proxy_headers=True, forwarded_allow_ips="127.0.0.1")
    elif args.command == "scheduler":
        Engine(settings).serve(args.lane)
    elif args.command == "run":
        # Keep the published artifact immutable: serve a copy, so the data directory is the
        # only thing that changes between runs.
        from .local_app import expand_env, local_env
        store = Store(settings.data / "factory.sqlite3")
        project = store.project(args.project)
        artifact = json.loads(project["artifact"] or "null")
        if project["state"] != "ready" or not artifact:
            raise SystemExit("Project has no verified artifact to run")
        # The artifact root already corresponds to `artifact_dir` (release/), so `serve_argv`
        # paths are relative to it. Joining them against the job directory looked for
        # release/release/server/index.mjs and failed to start.
        source = settings.data / "jobs" / artifact["job_id"] / "output"
        if not source.is_dir():
            raise SystemExit("The published artifact for this revision is missing")
        profile = settings.profiles[project["profile"]]
        argv = [a.replace("{port}", str(args.port)) for a in profile.serve_argv]
        # The verifier runs serve_argv with the project root as cwd, where `release/` exists.
        # The published artifact IS `release/`, so an argv path that starts with artifact_dir
        # must lose that prefix or node is asked for release/release/server/index.mjs.
        prefix = profile.artifact_dir.strip("/") + "/"
        if argv and argv[-1].startswith(prefix):
            argv[-1] = argv[-1][len(prefix):]
        entry = source / argv[-1] if argv else None
        if not argv or not entry.is_file():
            raise SystemExit(
                "serve_argv does not resolve inside the published artifact (looked for {})".format(
                    entry if argv else "<empty>"))
        data_dir = settings.data / "products" / project["id"] / "data"
        data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        env = local_env()
        env.update(expand_env(profile.env, data_dir=data_dir, app_dir=source))
        env.update({"PORT": str(args.port), "NITRO_PORT": str(args.port), "HOST": "127.0.0.1",
                    "NITRO_HOST": "127.0.0.1", "NUXT_TELEMETRY_DISABLED": "1"})
        print(json.dumps({"project": project["name"], "url": f"http://127.0.0.1:{args.port}",
                          "data": str(data_dir), "env": sorted(profile.env)}, indent=2), flush=True)
        os.chdir(source)
        os.execvpe(argv[0], argv, env)
    elif args.command == "backup":
        destination = Path(args.destination)
        if destination.exists():
            raise SystemExit("Refusing to overwrite an existing backup")
        Store(settings.data / "factory.sqlite3").backup(destination)
        print("SQLite snapshot verified. Back up jobs and memory separately while workers are stopped.")
    elif args.command == "doctor":
        report = {"omp": shutil.which(settings.omp), "model_configured": bool(settings.model),
                  "memorix": shutil.which(settings.memorix) if settings.memorix else "disabled",
                  "profiles": list(settings.profiles), "targets": list(settings.targets)}
        try:
            result = subprocess.run(["docker", "--host=unix:///var/run/docker.sock", "info", "--format", "{{.ServerVersion}}"],
                                    capture_output=True, timeout=10)
            report["docker_available"] = result.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            report["docker_available"] = False
        print(json.dumps(report, indent=2))
    elif args.command == "reconcile":
        from .deploy import Deployer
        store = Store(settings.data / "factory.sqlite3")
        row = store.one("SELECT * FROM deployments WHERE id=?", (args.deployment,))
        if row["state"] != "unknown":
            raise SystemExit("Only an unknown deployment may be reconciled")
        target = settings.targets[row["project_id"] + ":" + row["environment"]]
        receipt = Deployer().status(target)
        if receipt.get("status") != "healthy":
            raise SystemExit("Remote state is not healthy; recover it on the target before reconciling")
        state = "succeeded" if receipt.get("digest") == row["digest"] else "failed"
        with store.tx() as db:
            db.execute("UPDATE deployments SET state=?,receipt=? WHERE id=? AND state='unknown'",
                       (state, json.dumps(receipt), row["id"]))
            store.event_tx(db, row["project_id"], row["job_id"], "deployment.reconciled", {"state": state, "receipt": receipt})
        print(json.dumps({"state": state, "receipt": receipt}))


if __name__ == "__main__":
    main()
