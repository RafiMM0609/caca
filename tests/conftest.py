from pathlib import Path

import pytest

from factory.config import Settings
from factory.db import Store
from factory.models import Plan, Profile, ProjectInput


IMAGE = "python@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df"
ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def settings(tmp_path):
    config = Settings(data=tmp_path, operator_token="o" * 40, release_token="r" * 40,
                      model="test/model", pi="/usr/bin/true",
                      profiles={"static": Profile(image=IMAGE, verify_argv=["python3", "/checks/static.py"],
                                                   checks_dir=str(ROOT / "checks"))})
    config.validate()
    return config


@pytest.fixture
def store(settings):
    return Store(settings.data / "factory.sqlite3")


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

