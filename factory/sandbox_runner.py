"""Executed INSIDE the disposable verifier container, never on the controller host."""
import base64
import io
import json
import os
import resource
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path


def main():
    resource.setrlimit(resource.RLIMIT_FSIZE, (32_000_000, 32_000_000))
    shutil.copytree("/source", "/work/app")
    os.chdir("/work/app")
    with open("/tmp/verify.log", "wb") as log:
        result = subprocess.run(sys.argv[1:], stdout=log, stderr=subprocess.STDOUT)
    if result.returncode:
        raise SystemExit(result.returncode)
    root = Path("/output")
    stream = io.BytesIO()
    total = 0
    count = 0
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        for directory, dirs, files in os.walk(root, followlinks=False):
            for name in dirs + files:
                path = Path(directory) / name
                if path.is_symlink() or not (path.is_file() or path.is_dir()):
                    raise ValueError("Invalid artifact entry")
            for name in sorted(files):
                path = Path(directory) / name
                total += path.stat().st_size
                count += 1
                if total > 20_000_000 or count > 10000:
                    raise ValueError("Artifact exceeds 20 MB or 10000 files")
                archive.add(path, arcname=path.relative_to(root).as_posix(), recursive=False)
    if count == 0:
        raise ValueError("Verification must export an artifact into /output")
    print(json.dumps({"artifact": base64.b64encode(stream.getvalue()).decode(),
                      "log": Path("/tmp/verify.log").read_text(errors="replace")[-100000:]}))


if __name__ == "__main__":
    main()

