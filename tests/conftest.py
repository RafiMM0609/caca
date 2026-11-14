from pathlib import Path

import pytest

from factory.config import Settings
from factory.db import Store
from factory.models import Plan, Profile, ProjectInput


ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def settings(tmp_path):
    # A native profile. Docker support was removed, so every fixture that used an image and
    # `verify_argv` now declares `commands` and `checks_argv` instead. `commands=[['true']]`
    # produces no files, which is why `artifact_dir` names the source file the fixture projects
    # already carry rather than a build output directory.
    config = Settings(data=tmp_path, operator_token="o" * 40, release_token="r" * 40,
                      model="test/model", pi="/usr/bin/true",
                      profiles={"static": Profile(runner="native",
                                                   commands=[["true"]],
                                                   artifact_dir="index.html",
                                                   checks_dir=str(ROOT / "checks"),
                                                   checks_argv=[["python3", str(ROOT / "checks" / "static.py")]])})
    config.validate()
    return config


@pytest.fixture
def store(settings):
    return Store(settings.data / "factory.sqlite3")


@pytest.fixture
def client(settings):
    """An authenticated API client.

    Defined here rather than in one test module because more than one module now exercises the
    API surface, and a second copy of this fixture would drift from this one.
    """
    from fastapi.testclient import TestClient

    from factory.api import create_app

    with TestClient(create_app(settings)) as client:
        client.headers["Authorization"] = "Bearer " + settings.operator_token
        yield client


@pytest.fixture
def plan():
    return Plan(summary="Create a simple accessible portfolio website.", assumptions=[], questions=[],
                tasks=[{"id": "website", "title": "Build website", "instructions": "Create accessible HTML and CSS.",
                        "acceptance": ["Homepage has a title and visible contact information"], "depends_on": []}])


@pytest.fixture
def project(store, plan):
    project = store.create_project(ProjectInput(name="Test portfolio", brief="Create a portfolio with visible contact information."))
    with store.tx() as db:
        db.execute("UPDATE projects SET plan=?,revision=1,state='awaiting_plan' WHERE id=?",
                   (plan.model_dump_json(), project["id"]))
    return store.project(project["id"])

