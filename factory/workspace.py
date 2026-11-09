import hashlib
import io
import json
import os
import shutil
import tarfile
from pathlib import Path

from .models import safe_path

# Native artifact ceiling. The Docker sandbox keeps its own 20 MB limit because /output is
# a 64 MB tmpfs; native execution has no tmpfs to protect and bounds itself here instead.
MAX_ARTIFACT = 150_000_000
MAX_ARTIFACT_FILES = 10000


def files_under(root: Path, limit=MAX_ARTIFACT, *, allow_git: bool = False,
                allow_symlinks: bool = False):
    """Walk a tree, yielding regular files and validating every path.

    ``allow_git`` and ``allow_symlinks`` exist for the **artifact packaging** path only.
    Generated *source* keeps both disabled: a worker must not create a `.git` directory or
    a link, and the call sites that move source do not pass these flags.

    An artifact, by contrast, is assembled by this factory from tracked files, and its
    `.git` is created by `git init` inside the artifact directory. Links are accepted there
    because a production build legitimately emits them (Nitro links its server bundle's
    dependencies), and rejecting them failed a correct application at publication time.
    """
    total = 0
    count = 0
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            item = Path(directory) / name
            relative = item.relative_to(root).as_posix()
            safe_path(relative, allow_git=allow_git)
            if item.is_symlink():
                if not allow_symlinks:
                    raise ValueError("Symlinks are not allowed in source or artifacts")
                continue
            if not item.is_dir() and not item.is_file():
                raise ValueError("Only regular files and directories are allowed")
        for name in sorted(files):
            item = Path(directory) / name
            if item.is_symlink() and not allow_symlinks:
                raise ValueError("Symlinks are not allowed in source or artifacts")
            total += item.stat().st_size
            count += 1
            if total > limit or count > MAX_ARTIFACT_FILES:
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


def digest_file(path):
    hasher = hashlib.sha256()
    with open(path, "rb") as file:
        for chunk in iter(lambda: file.read(65536), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def package(root: Path, destination: Path):
    """Archive a published artifact.

    The artifact is the source of the product plus its own freshly created git history, so
    this path allows `.git` and symlinks. Symlinks are stored as links (`SYMTYPE`) rather
    than dereferenced or silently dropped: dereferencing would duplicate megabytes of a
    shared Nitro bundle, and dropping them would ship an application that cannot start.
    """
    if not root.is_dir() or root.is_symlink():
        raise ValueError("Build did not produce the configured artifact directory")
    files = sorted(files_under(root, allow_git=True, allow_symlinks=True))
    if not files:
        raise ValueError("Empty artifact")
    with tarfile.open(destination, "w:gz") as archive:
        for path in files:
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                target = os.readlink(path)
                # A link that is absolute or climbs out of the artifact would hand an
                # extractor a path outside its destination. Legitimate build links are
                # always relative and stay inside the tree, so reject anything else at
                # packaging time rather than relying on the extractor to catch it.
                if os.path.isabs(target) or "\\" in target:
                    raise ValueError(f"Artifact symlink must be relative: {relative}")
                resolved = (path.parent / target).resolve()
                if not resolved.is_relative_to(root.resolve()):
                    raise ValueError(f"Artifact symlink escapes the artifact: {relative}")
                info = tarfile.TarInfo(relative)
                info.type = tarfile.SYMTYPE
                info.linkname = target
                info.mode = 0o777
                archive.addfile(info)
                continue
            archive.add(path, arcname=relative, recursive=False)
    if destination.stat().st_size > MAX_ARTIFACT:
        raise ValueError("Compressed artifact exceeds size limit")
    return {"digest": digest_file(destination), "bytes": destination.stat().st_size,
            "files": len(files)}


def atomic_json(path: Path, value):
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(value, indent=2))
    temp.replace(path)


def clone_tree(source: Path, target: Path):
    """Copy a source tree that must contain only regular files.

    Strict on purpose: this is the path that moves *generated application source* between
    owner-controlled directories, so a symlink there is a defect and is rejected.
    """
    list(files_under(source))
    shutil.copytree(source, target)


def clone_build_tree(source: Path, target: Path):
    """Copy an already-built tree for one acceptance case, preserving symlinks.

    This is deliberately laxer than :func:`clone_tree` and must not be confused with it.
    A **build** tree legitimately contains symlinks: `node_modules/.bin/*` is nothing but
    symlinks, and Nuxt/Nitro link assets inside their output. The strict
    :func:`files_under` walk rejects any symlink, so reusing it here would fail every real
    Node verification at the per-case copy step.

    The safety burden stays where it belongs: this tree was produced by the verifier from
    the approved product source, it is never tarred into the artifact (that path uses
    ``package()`` and ``files_under``), and it is deleted with the scratch directory.
    Symlinks are copied as links, never dereferenced, so a link cannot pull an outside file
    into the tree.
    """
    if not source.is_dir() or source.is_symlink():
        raise ValueError("Build tree source must be a real directory")
    # dirs_exist_ok is safe here: the destination is a fresh per-case path.
    shutil.copytree(source, target, symlinks=True, dirs_exist_ok=True)


def extract(data: bytes, destination: Path):
    """Extract a bounded artifact; never use tar.extractall.

    A published artifact contains the product's own `.git` and may contain symlinks that a
    production build emitted, so both are accepted here. Links are re-created as links after
    their target is validated: it must be relative and must stay inside the destination, so
    an artifact can never point at `/etc/passwd` or an operator's home directory. The
    20 MB ceiling this used to carry belonged to the Docker sandbox's tmpfs, not to native
    extraction, and it rejected a real 29 MB build output.
    """
    total = 0
    seen = set()
    destination_root = destination.resolve()
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        for member in archive:
            safe_path(member.name, allow_git=True)
            if member.name in seen:
                raise ValueError("Artifact contains duplicate entries")
            seen.add(member.name)
            path = destination / member.name
            if member.issym():
                target = member.linkname
                if os.path.isabs(target) or "\\" in target:
                    raise ValueError(f"Artifact symlink must be relative: {member.name}")
                resolved = (path.parent / target).resolve()
                if not resolved.is_relative_to(destination_root):
                    raise ValueError(f"Artifact symlink escapes the artifact: {member.name}")
                path.parent.mkdir(parents=True, exist_ok=True)
                if path.exists() or path.is_symlink():
                    raise ValueError("Artifact contains duplicate entries")
                path.symlink_to(target)
                continue
            if not member.isfile():
                raise ValueError("Artifact contains non-regular entries")
            total += member.size
            if total > MAX_ARTIFACT or len(seen) > MAX_ARTIFACT_FILES:
                raise ValueError("Artifact exceeds extraction limits")
            path.parent.mkdir(parents=True, exist_ok=True)
            with archive.extractfile(member) as source, path.open("xb") as target_file:
                shutil.copyfileobj(source, target_file)
            path.chmod(0o755 if member.mode & 0o111 else 0o644)
    if not seen:
        raise ValueError("Empty artifact")

