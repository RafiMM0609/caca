import json

import pytest
from fastapi.testclient import TestClient

from factory.api import create_app
from factory.models import Target


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings)) as client:
        client.headers["Authorization"] = "Bearer " + settings.operator_token
        yield client


def test_auth_headers_and_xss_safe_ui(client):
    assert client.get("/api/projects", headers={"Authorization": ""}).status_code == 401
    response = client.get("/")
    assert response.status_code == 200
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
    assert response.headers["cache-control"] == "no-store"
    assert client.get("/assets/app.js").status_code == 200


def test_bad_input_origin_and_body_limit(client):
    assert client.post("/api/projects", json={"name": "a", "brief": "short"}).status_code == 422
    assert client.post("/api/projects", json={}, headers={"Origin": "https://evil.test"}).status_code == 403
    assert client.post("/api/projects", content="x" * 256001).status_code == 413


def test_plan_roundtrip_conflict_and_approval(client, project, plan):
    base = "/api/projects/" + project["id"]
    assert client.put(base + "/plan", json={"revision": 0, "plan": plan.model_dump()}).status_code == 409
    response = client.put(base + "/plan", json={"revision": 1, "plan": plan.model_dump()})
    assert response.status_code == 200
    assert response.json()["revision"] == 2
    assert client.post(base + "/approve?revision=1").status_code == 409
    assert client.post(base + "/approve?revision=2").status_code == 202
    assert client.put(base + "/plan", json={"revision": 2, "plan": plan.model_dump()}).status_code == 409


def test_release_separation_and_exact_staging_gate(client, store, settings, project):
    target = Target(host="release@app.example.test", identity_file="/keys/test", known_hosts="/keys/known_hosts")
    settings.targets[project["id"] + ":staging"] = target
    settings.targets[project["id"] + ":production"] = target
    digest = "a" * 64
    with store.tx() as db:
        db.execute("UPDATE projects SET state='ready',artifact=? WHERE id=?", (json.dumps({"digest": digest}), project["id"]))
    path = "/api/projects/" + project["id"] + "/deploy"
    body = {"environment": "production", "digest": digest}
    assert client.post(path, json=body).status_code == 403
    release = {"Authorization": "Bearer " + settings.release_token}
    assert client.post(path, json=body, headers=release).status_code == 409
    assert client.post("/api/projects", json={"name": "test", "brief": "x" * 30}, headers=release).status_code == 403
    with store.tx() as db:
        db.execute("INSERT INTO deployments VALUES('stage',?,'test','staging',?,'succeeded','{}',0)", (project["id"], digest))
    assert client.post(path, json={**body, "digest": "b" * 64}, headers=release).status_code == 409
    assert client.post(path, json=body, headers=release).status_code == 202
    assert store.claim() is None
    assert store.claim("deploy")["kind"] == "deploy"


def test_missing_records_and_unknown_profiles(client):
    assert client.get("/api/projects/missing").status_code == 404
    assert client.post("/api/projects", json={"name": "new app", "brief": "x" * 30, "profile": "root"}).status_code == 422


def test_failed_auth_rate_limit(client):
    for _ in range(20):
        assert client.get("/api/status", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/api/status", headers={"Authorization": "Bearer wrong"}).status_code == 429



def test_the_source_endpoint_survives_binary_assets(client, settings, store, project):
    """A product built from the starter ships fonts, so this endpoint must not assume text.

    Measured regression: the endpoint read every file with `read_text()`, so the first woff2
    file raised `UnicodeDecodeError` and the dashboard's source view failed for every product
    built from the starter. The endpoint had no test at all before this one.
    """
    job_id = "job" + "0" * 29
    source = settings.data / "jobs" / job_id / "source"
    (source / "public" / "assets" / "fonts").mkdir(parents=True)
    (source / "app.vue").write_text("<template><p>ok</p></template>")
    (source / "public" / "assets" / "fonts" / "a.woff2").write_bytes(b"\x00wOF2\xff\xfe binary")
    with store.tx() as db:
        db.execute("UPDATE projects SET artifact=? WHERE id=?",
                   (json.dumps({"job_id": job_id, "digest": "d" * 64}), project["id"]))

    response = client.get("/api/projects/" + project["id"] + "/source")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["app.vue"] == "<template><p>ok</p></template>"
    assert "(omitted)" in body and "a.woff2" in body["(omitted)"]


def test_the_source_endpoint_reports_a_missing_artifact(client, project):
    assert client.get("/api/projects/" + project["id"] + "/source").status_code == 404
