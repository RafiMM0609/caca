import hashlib
import io
import json
import os
import shutil
import tarfile
from pathlib import Path

from .models import Change, safe_path

MAX_ARTIFACT = 100_000_000


def files_under(root: Path, limit=MAX_ARTIFACT):
    total = 0
    count = 0
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            item = Path(directory) / name
            relative = item.relative_to(root).as_posix()
            safe_path(relative)
            if item.is_symlink():
                raise ValueError("Symlinks are not allowed in source or artifacts")
            if not item.is_dir() and not item.is_file():
                raise ValueError("Only regular files and directories are allowed")
        for name in sorted(files):
            item = Path(directory) / name
            total += item.stat().st_size
            count += 1
            if total > limit or count > 10000:
                raise ValueError("Workspace size/file count limit exceeded")
            yield item


def snapshot(root: Path):
    # Lockfiles are retained on disk for reproducibility, omitted from model/UI text context.
    locks = {"package-lock.json", "pnpm-lock.yaml", "yarn.lock", "bun.lock"}
    result = {p.relative_to(root).as_posix(): p.read_text() for p in files_under(root, limit=2_000_000)
              if p.name not in locks}
    if sum(len(value.encode()) for value in result.values()) > 300_000:
        raise ValueError("Text context exceeds 300 KB (excluding lockfiles)")
    return result


def apply(root: Path, change: Change):
    # The tree is created by this controller and is never mounted writable into a verifier.
    list(files_under(root))
    for name in change.delete:
        path = root / name
        if path.exists():
            if not path.is_file():
                raise ValueError("Deletion must name a regular file")
            path.unlink()
    for name, content in change.files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    snapshot(root)  # Reject oversized/nontext trees before committing the task.


def digest_file(path):
    hasher = hashlib.sha256()
    with open(path, "rb") as file:
        for chunk in iter(lambda: file.read(65536), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def package(root: Path, destination: Path):
    if not root.is_dir() or root.is_symlink():
        raise ValueError("Build did not produce the configured artifact directory")
    files = sorted(files_under(root))
    if not files:
        raise ValueError("Empty artifact")
    with tarfile.open(destination, "w:gz") as archive:
        for path in files:
            archive.add(path, arcname=path.relative_to(root).as_posix(), recursive=False)
    if destination.stat().st_size > MAX_ARTIFACT:
        raise ValueError("Compressed artifact exceeds size limit")
    return {"digest": digest_file(destination), "bytes": destination.stat().st_size, "files": len(files)}


def atomic_json(path: Path, value):
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(value, indent=2))
    temp.replace(path)


def clone_tree(source: Path, target: Path):
    list(files_under(source))
    shutil.copytree(source, target)


def extract(data: bytes, destination: Path):
    """Extract only bounded regular files; never use tar.extractall on worker output."""
    total = 0
    seen = set()
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        for member in archive:
            safe_path(member.name)
            if not member.isfile() or member.name in seen:
                raise ValueError("Artifact contains non-regular or duplicate entries")
            seen.add(member.name)
            total += member.size
            if total > 20_000_000 or len(seen) > 10000:
                raise ValueError("Artifact exceeds extraction limits")
            path = destination / member.name
            path.parent.mkdir(parents=True, exist_ok=True)
            with archive.extractfile(member) as source, path.open("xb") as target:
                shutil.copyfileobj(source, target)
            path.chmod(0o755 if member.mode & 0o111 else 0o644)
    if not seen:
        raise ValueError("Empty artifact")

