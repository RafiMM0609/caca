"""Everything one client owns, exported as one archive.

Attribution exists so a client's products can be handed over — or found for retirement —
in one deliberate operation. These tests prove the archive is complete and honest: every
project with a repository ships a bundle that clones, and a project without one is named
as absent rather than silently omitted.
"""
import json
import subprocess
import tarfile

import pytest
from fastapi.testclient import TestClient

from factory import products
from factory.api import create_app
from factory.config import Settings
from factory.db import Store

IMAGE = "python@sha256:" + "b" * 64


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


def _project(client, name, brief, client_id="acme-corp"):
    response = client.post("/api/projects", json={"name": name, "brief": brief,
                                                  "profile": "static",
                                                  "client_id": client_id})
    assert response.status_code == 201, response.text
    return response.json()


def _with_repo(settings, project, commit="Initial work"):
    root = products.ensure_repo(settings.data, project["id"])
    (root / "index.html").write_text("<title>handoff</title>\n")
    products.commit_all(root, commit)
    return root


def test_inventory_lists_every_project_of_one_client(settings, client):
    first = _project(client, "Portal", "A portal attributed to the client.")
    second = _project(client, "Faktur", "An invoicing tool for the same client.")
    _project(client, "Lain", "A product attributed to a different client.",
             client_id="other-co")
    _with_repo(settings, second)

    response = client.get("/api/clients/acme-corp/inventory")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["client_id"] == "acme-corp"
    ids = [row["project"] for row in body["projects"]]
    assert ids == [first["id"], second["id"]], "only this client's projects, oldest first"
    assert body["projects"][1]["repository"] is True
    assert body["projects"][0]["repository"] is False


def test_inventory_of_an_unknown_client_is_a_404(client):
    assert client.get("/api/clients/nobody/inventory").status_code == 404


def test_inventory_rejects_an_invalid_client_id(client):
    assert client.get("/api/clients/Acme Corp/inventory").status_code == 422


def test_archive_ships_every_repository_and_a_manifest(settings, client, tmp_path):
    first = _project(client, "Portal", "A portal attributed to the client.")
    second = _project(client, "Faktur", "An invoicing tool for the same client.")
    _with_repo(settings, first)
    _with_repo(settings, second, commit="Second product")

    response = client.get("/api/clients/acme-corp/archive")
    assert response.status_code == 200, response.text

    archive = tmp_path / "acme.tar.gz"
    archive.write_bytes(response.content)
    with tarfile.open(archive, "r:gz") as tar:
        names = tar.getnames()
        manifest = json.loads(tar.extractfile("client-acme-corp/manifest.json").read())
        bundles = {name: tar.extractfile(name).read() for name in names
                   if name.endswith(".bundle")}
    assert "client-acme-corp/manifest.json" in names
    assert sorted(bundles) == sorted([f"client-acme-corp/{first['id']}.bundle",
                                      f"client-acme-corp/{second['id']}.bundle"])

    # The manifest names every project, including what is inside each bundle.
    listed = {entry["project"]: entry for entry in manifest["projects"]}
    assert set(listed) == {first["id"], second["id"]}
    assert listed[first["id"]]["bundle"]["digest"]
    assert listed[first["id"]]["bundle"]["branches"]

    # Each bundle still clones with its own history: a bundle that does not clone is not a
    # hand-off, it is a corrupt file with a manifest attached.
    for offset, project in enumerate((first, second)):
        blob = bundles[f"client-acme-corp/{project['id']}.bundle"]
        one = tmp_path / f"one-{offset}.bundle"
        one.write_bytes(blob)
        clone = tmp_path / f"clone-{offset}"
        result = subprocess.run(["git", "clone", "-q", str(one), str(clone)],
                                capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        log = subprocess.run(["git", "log", "--oneline"], cwd=clone,
                             capture_output=True, text=True).stdout
        assert ("Second product" if offset else "Initial work") in log


def test_archive_names_a_project_that_has_no_repository(settings, client, tmp_path):
    built = _project(client, "Sudah Jadi", "A product with a repository.")
    _with_repo(settings, built)
    _project(client, "Belum Jadi", "A product that has never built.")

    response = client.get("/api/clients/acme-corp/archive")
    assert response.status_code == 200, response.text

    archive = tmp_path / "acme.tar.gz"
    archive.write_bytes(response.content)
    with tarfile.open(archive, "r:gz") as tar:
        names = tar.getnames()
        manifest = json.loads(tar.extractfile("client-acme-corp/manifest.json").read())
    entries = {entry["project"]: entry for entry in manifest["projects"]}
    assert entries[built["id"]]["bundle"] is not None
    assert entries[built["id"]]["bundle"]["digest"]
    assert len(entries) == 2, "the manifest must name both projects"
    pending = [e for e in entries.values() if e["project"] != built["id"]][0]
    assert pending["bundle"] is None, "a project with no repository says so"
    assert [n for n in names if n.endswith(".bundle")], "the built product still ships"


def test_archive_of_an_unknown_client_is_a_404(client):
    assert client.get("/api/clients/nobody/archive").status_code == 404


def test_archive_is_recorded_in_the_audit_trail(settings, client):
    built = _project(client, "Terekam", "A product whose archive must be auditable.")
    _with_repo(settings, built)
    assert client.get("/api/clients/acme-corp/archive").status_code == 200

    store = Store(settings.data / "factory.sqlite3")
    events = [e for e in store.all("SELECT kind, detail FROM events")
              if e["kind"] == "client.archived"]
    assert events, "an archive must be visible in the audit trail"
    detail = json.loads(events[0]["detail"])
    assert detail["client_id"] == "acme-corp"
    assert built["id"] in detail["projects"]
    assert detail["bytes"] > 0


def test_archive_requires_the_operator_token(settings, client):
    built = _project(client, "Terkunci", "A product whose archive must not be public.")
    _with_repo(settings, built)
    anonymous = TestClient(create_app(settings))
    assert anonymous.get("/api/clients/acme-corp/archive").status_code == 401
    assert anonymous.get("/api/clients/acme-corp/inventory").status_code == 401
