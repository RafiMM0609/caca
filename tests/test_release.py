import hashlib
import importlib.machinery
import importlib.util
import io
import json
import tarfile
from pathlib import Path

import pytest

from factory.db import Conflict
from factory.engine import Engine
from factory.models import Target


loader = importlib.machinery.SourceFileLoader("sf_release", str(Path(__file__).parents[1] / "deploy" / "sf-release"))
spec = importlib.util.spec_from_loader(loader.name, loader)
release = importlib.util.module_from_spec(spec)
loader.exec_module(release)


def artifact(content=b"hello"):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        item = tarfile.TarInfo("index.html")
        item.size = len(content)
        archive.addfile(item, io.BytesIO(content))
    data = stream.getvalue()
    return data, hashlib.sha256(data).hexdigest()


def test_atomic_release_idempotency_and_rollback(tmp_path, monkeypatch):
    config = {"root": str(tmp_path), "health_url": "http://localhost/"}
    monkeypatch.setattr(release, "health", lambda _: True)
    first, first_sha = artifact()
    result = release.operate(config, "apply", first_sha, first)
    assert result["status"] == "healthy"
    assert release.current_digest(tmp_path) == first_sha
    assert release.operate(config, "apply", first_sha, first)["idempotent"]
    second, second_sha = artifact(b"broken")
    monkeypatch.setattr(release, "health", lambda _: release.current_digest(tmp_path) == first_sha)
    with pytest.raises(RuntimeError):
        release.operate(config, "apply", second_sha, second)
    assert release.current_digest(tmp_path) == first_sha
    assert json.loads((tmp_path / "release.json").read_text())["status"] == "rolled_back"


def test_checksum_and_archive_attack(tmp_path, monkeypatch):
    config = {"root": str(tmp_path), "health_url": "http://localhost/"}
    with pytest.raises(ValueError, match="checksum"):
        release.operate(config, "apply", "0" * 64, b"wrong")
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        archive.addfile(tarfile.TarInfo("../escape"))
    data = stream.getvalue()
    with pytest.raises(ValueError, match="Unsafe"):
        release.operate(config, "apply", hashlib.sha256(data).hexdigest(), data)
    assert not (tmp_path / "current").exists()


def test_interrupted_release_requires_explicit_recovery(tmp_path, monkeypatch):
    config = {"root": str(tmp_path), "health_url": "http://localhost/"}
    monkeypatch.setattr(release, "health", lambda _: True)
    data, digest = artifact()
    release.operate(config, "apply", digest, data)
    release.write_json(tmp_path / "release.json", {"status": "deploying", "digest": digest, "previous": None})
    assert release.operate(config, "status")["status"] == "unknown"
    with pytest.raises(ValueError, match="Recover"):
        release.operate(config, "apply", digest, data)
    release.operate(config, "recover")
    assert release.current_digest(tmp_path) is None


def test_deploy_failure_blocks_blind_retry(settings, store, project):
    digest = "a" * 64
    settings.targets[project["id"] + ":staging"] = Target(host="release@host.test", identity_file="/keys/test", known_hosts="/keys/known")
    with store.tx() as db:
        db.execute("UPDATE projects SET state='ready',artifact=? WHERE id=?", (json.dumps({"digest": digest, "job_id": "build"}), project["id"]))
    class LostConnection:
        def apply(self, *args):
            raise ConnectionError("Lost SSH after remote release may have succeeded")
    payload = {"environment": "staging", "digest": digest}
    store.enqueue(project["id"], "deploy", payload)
    job = store.claim("deploy")
    Engine(settings, store, deployer=LostConnection()).process(job, "deploy")
    assert store.one("SELECT * FROM deployments WHERE job_id=?", (job["id"],))["state"] == "unknown"
    with pytest.raises(Conflict, match="Reconcile"):
        store.enqueue(project["id"], "deploy", payload)

