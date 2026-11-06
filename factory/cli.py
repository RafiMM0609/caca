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
