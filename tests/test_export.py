"""Attribute each product to a client, and be able to hand the repository over.

Products are separate git repositories and the goal is to give each one to a different client.
Without attribution that is impossible to do deliberately, and without an export path the
hand-off is a manual copy of a directory that may include build output.
"""
import json
import subprocess

import pytest
from fastapi.testclient import TestClient

from factory import products
from factory.api import create_app
from factory.config import Settings
from factory.db import Store

IMAGE = "python@sha256:" + "a" * 64


@pytest.fixture
def settings(tmp_path):
    config = Settings(data=tmp_path, operator_token="o" * 40, release_token="r" * 40,
                      pi="/usr/bin/true", model="test/model",
                      profiles={"static": {"image": IMAGE, "verify_argv": ["true"],
                                           "checks_dir": str(tmp_path)}})
    config.validate()
    return config


@pytest.fixture
def client(settings):
    app = create_app(settings)
    with TestClient(app) as test_client:
        test_client.headers["Authorization"] = "Bearer " + settings.operator_token
        yield test_client


def test_a_project_records_the_client_it_is_for(settings, client):
    response = client.post("/api/projects", json={"name": "Portal Klien", "brief":
                           "Build a portal for one specific client to use.",
                           "profile": "static", "client_id": "acme-corp"})
    assert response.status_code == 201, response.text
    assert response.json()["client_id"] == "acme-corp"

    store = Store(settings.data / "factory.sqlite3")
    events = [e for e in store.all("SELECT kind, detail FROM events")
              if e["kind"] == "project.created"]
    assert json.loads(events[0]["detail"])["client_id"] == "acme-corp"


def test_client_id_is_optional(client):
    response = client.post("/api/projects", json={"name": "Tanpa Klien", "brief":
                           "A product that is not attributed to any client.",
                           "profile": "static"})
    assert response.status_code == 201, response.text
    assert response.json()["client_id"] == ""


@pytest.mark.parametrize("bad", ["Acme Corp", "-acme", "acme corp", "x" * 81])
def test_an_invalid_client_id_is_rejected(client, bad):
    response = client.post("/api/projects", json={"name": "Bad Client", "brief":
                           "A product whose client id does not meet the pattern.",
                           "profile": "static", "client_id": bad})
    assert response.status_code == 422, response.text


def test_the_repository_can_be_exported_as_a_cloneable_bundle(settings, client, tmp_path):
    created = client.post("/api/projects", json={"name": "Handoff Produk", "brief":
                          "A product that will be handed to its client.",
                          "profile": "static", "client_id": "acme-corp"}).json()
    root = products.ensure_repo(settings.data, created["id"])
    (root / "index.html").write_text("<title>handoff</title>\n")
    products.commit_all(root, "Initial work")

    response = client.get(f"/api/projects/{created['id']}/export")
    assert response.status_code == 200, response.text
    bundle = tmp_path / "export.bundle"
    bundle.write_bytes(response.content)
    assert bundle.stat().st_size > 0

    # A bundle is only useful if it clones and carries the history.
    clone = tmp_path / "cloned"
    result = subprocess.run(["git", "clone", "-q", str(bundle), str(clone)],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    log = subprocess.run(["git", "log", "--oneline"], cwd=clone,
                         capture_output=True, text=True).stdout
    assert "Initial work" in log
    assert (clone / "index.html").is_file()


def test_export_is_recorded_as_an_event(settings, client):
    created = client.post("/api/projects", json={"name": "Audit Export", "brief":
                          "A product whose export should be auditable.",
                          "profile": "static", "client_id": "acme-corp"}).json()
    products.ensure_repo(settings.data, created["id"])
    assert client.get(f"/api/projects/{created['id']}/export").status_code == 200

    store = Store(settings.data / "factory.sqlite3")
    events = [e for e in store.all("SELECT kind, detail FROM events WHERE project_id=?",
                                   (created["id"],)) if e["kind"] == "project.exported"]
    assert events, "an export must be visible in the audit trail"
    detail = json.loads(events[0]["detail"])
    assert detail["client_id"] == "acme-corp"
    assert detail["digest"]


def test_export_requires_the_operator_token(settings, client):
    created = client.post("/api/projects", json={"name": "Guarded Export", "brief":
                          "A product whose history must not be public.",
                          "profile": "static"}).json()
    products.ensure_repo(settings.data, created["id"])
    anonymous = TestClient(create_app(settings))
    response = anonymous.get(f"/api/projects/{created['id']}/export")
    assert response.status_code == 401


def test_export_of_a_project_without_a_repository_is_a_404(client):
    created = client.post("/api/projects", json={"name": "No Repo Yet", "brief":
                          "A product that has never been built.",
                          "profile": "static"}).json()
    assert client.get(f"/api/projects/{created['id']}/export").status_code == 404
