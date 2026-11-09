"""Pi as a real coding harness inside an isolated worker environment.

The environment tests never call a model. The harness test needs a live provider and
is skipped unless FACTORY_LIVE_WORKER=1, so the default suite stays offline.
"""
import os
from pathlib import Path

import pytest

from factory.agents import PiWorker, PiWorkerError
from factory.config import Settings


@pytest.fixture
def settings(tmp_path):
    return Settings(data=tmp_path, operator_token="o" * 40, release_token="r" * 40,
                    model="test/model", pi="/usr/bin/true")


@pytest.fixture
def worker(settings):
    return PiWorker(settings)


def test_home_is_isolated_inside_the_worker_directory(worker, tmp_path):
    home = worker.isolated_home(tmp_path / "worker")
    assert home == tmp_path / "worker" / "home"
    assert home.is_dir()
    assert (home / ".pi").is_dir()
    # The real operator home must never be used, or ~/.ssh and ~/.aws become reachable.
    assert str(home) != str(Path.home())


def test_worker_environment_cannot_see_factory_or_operator_secrets(worker, tmp_path, monkeypatch):
    monkeypatch.setenv("FACTORY_OPERATOR_TOKEN", "must-not-leak")
    monkeypatch.setenv("FACTORY_RELEASE_TOKEN", "must-not-leak-either")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "operator@example.test")
    home = worker.isolated_home(tmp_path / "worker")
    env = worker.worker_env(home)

    for withheld in ("FACTORY_OPERATOR_TOKEN", "FACTORY_RELEASE_TOKEN", "GIT_AUTHOR_EMAIL"):
        assert withheld not in env
        # And nothing carries the value under another name.
        assert "must-not-leak" not in " ".join(env.values())


def test_worker_environment_keeps_provider_credentials(worker, tmp_path, monkeypatch):
    """The model call needs the credential, so it must survive isolation."""
    monkeypatch.setenv("OPENCODE_API_KEY", "provider-key-value")
    env = worker.worker_env(worker.isolated_home(tmp_path / "worker"))
    assert env["OPENCODE_API_KEY"] == "provider-key-value"


def test_worker_directories_are_redirected_into_the_isolated_home(worker, tmp_path):
    home = worker.isolated_home(tmp_path / "worker")
    env = worker.worker_env(home)
    for name in ("HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "PI_CODING_AGENT_DIR"):
        assert env[name].startswith(str(home)), name


def test_worker_requires_a_model(worker, tmp_path):
    worker.settings.model = ""
    with pytest.raises(ValueError, match="FACTORY_MODEL"):
        worker.run_task(tmp_path, "do the task")


def test_worker_surfaces_process_failure_as_worker_error(worker, tmp_path, monkeypatch):
    monkeypatch.setattr("factory.agents.run", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("exit code 1")))
    with pytest.raises(PiWorkerError, match="exit code 1"):
        worker.run_task(tmp_path, "do the task")


def test_worker_reports_missing_output(worker, tmp_path, monkeypatch):
    monkeypatch.setattr("factory.agents.run", lambda *a, **k: '{"type":"error","errorMessage":"quota"}')
    with pytest.raises(PiWorkerError, match="quota"):
        worker.run_task(tmp_path, "do the task")


@pytest.mark.skipif(os.environ.get("FACTORY_LIVE_WORKER") != "1",
                    reason="set FACTORY_LIVE_WORKER=1 to run a real model worker")
def test_live_worker_edits_a_real_worktree(tmp_path):
    from factory import products

    settings = Settings(data=tmp_path / "data", operator_token="o" * 40, release_token="r" * 40,
                        model=os.environ.get("FACTORY_MODEL", ""),
                        pi=os.environ.get("FACTORY_PI", "pi"), agent_timeout=180)
    root = products.ensure_repo(settings.data, "live",
                                seed_files={"task.md": "Create out.txt containing exactly: done\n"})
    base = products.head_commit(root)
    work = products.worktree_add(root, settings.data / "wt" / "w", "worker/live")
    PiWorker(settings).run_task(work, "Read task.md and do exactly what it says.")
    products.commit_all(work, "live worker change")
    assert products.changed_files(work, base) == ["out.txt"]
    assert (work / "out.txt").read_text().strip() == "done"
