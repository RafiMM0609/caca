"""A verified build output can be served on a local port.

Serving is not a second build: the verifier already ran `npm ci` and `npm run build` and copied
`profile.artifact_dir` to `data/jobs/<job>/output/`. This takes a copy of that and keeps a
process alive.

Two things these tests exist to protect:

- A copy is mandatory. `Engine.verify` deletes `output/` before the next verification, so an app
  served from there dies on rebuild.
- A `serve` job must never be claimed by the build lane, which finishes each job and is expected
  to exit. Before this change every non-deploy job went to that lane.
"""
import json
import shutil

import pytest
from fastapi.testclient import TestClient

from factory import serving
from factory.api import create_app
from factory.config import Settings
from factory.db import Store, lane_predicate
from factory.models import Profile

IMAGE = "python@sha256:" + "a" * 64


def servable_profile():
    return Profile.model_validate({
        "runner": "native", "commands": [["true"]], "artifact_dir": "release",
        "serve_argv": ["node", "server/index.mjs"], "readiness_path": "/api/health",
        "env": {"DATABASE_URL": "file:{data}/app.db", "DATA_DIR": "{data}"},
    })


# -- the refusal paths ---------------------------------------------------------------

def test_a_profile_without_serve_argv_is_refused_by_name():
    profile = Profile.model_validate({"runner": "native", "commands": [["true"]],
                                      "artifact_dir": "release"})
    ok, reason = serving.servable(profile)
    assert not ok
    assert "serve_argv" in reason, "the operator must be told which field is missing"


def test_a_tester_profile_is_servable():
    ok, reason = serving.servable(servable_profile())
    assert ok, reason


def test_missing_output_is_reported_as_a_message_not_a_traceback(tmp_path):
    settings = Settings(data=tmp_path, operator_token="o" * 40, release_token="r" * 40)
    with pytest.raises(serving.ServeError, match="no output directory"):
        serving.stage(settings, "p1", "no-such-job", servable_profile())


# -- staging -------------------------------------------------------------------------

def test_staging_copies_the_output_and_owns_its_own_lifecycle(tmp_path):
    """Copied, not referenced: the job's output is deleted on the next verification."""
    settings = Settings(data=tmp_path, operator_token="o" * 40, release_token="r" * 40)
    source = tmp_path / "jobs" / "job-1" / "output"
    (source / "server").mkdir(parents=True)
    (source / "server" / "index.mjs").write_text("// built\n")

    tree = serving.stage(settings, "p1", "job-1", servable_profile())
    assert (tree / "server" / "index.mjs").is_file()

    shutil.rmtree(source)
    assert (tree / "server" / "index.mjs").is_file(), \
        "the served copy must survive deletion of the job output"


def test_staging_replaces_a_previous_copy(tmp_path):
    settings = Settings(data=tmp_path, operator_token="o" * 40, release_token="r" * 40)
    source = tmp_path / "jobs" / "job-1" / "output"
    source.mkdir(parents=True)
    (source / "old.txt").write_text("from the first build\n")
    tree = serving.stage(settings, "p1", "job-1", servable_profile())
    assert (tree / "old.txt").is_file()

    (source / "old.txt").unlink()
    (source / "new.txt").write_text("from the second build\n")
    tree = serving.stage(settings, "p1", "job-1", servable_profile())
    assert (tree / "new.txt").is_file()
    assert not (tree / "old.txt").exists(), "a stale file must not survive a replacement"


# -- lane isolation ------------------------------------------------------------------

def test_the_build_lane_never_claims_a_serve_job():
    """A serve job in the build lane would be started and then exit with the lane."""
    assert "serve" not in lane_predicate("build")
    assert "build" not in lane_predicate("serve")
    assert "'serve'" in lane_predicate("serve")


def test_the_build_lane_still_claims_build_plan_and_verify():
    predicate = lane_predicate("build")
    for kind in ("build", "plan", "verify"):
        assert f"'{kind}'" in predicate, f"the build lane must still claim {kind}"


def test_an_unknown_lane_is_refused():
    with pytest.raises(ValueError, match="Unknown lane"):
        lane_predicate("nonsense")


def test_deploy_stays_in_its_own_lane():
    assert lane_predicate("deploy") == "kind IN ('deploy')"


def test_a_queued_serve_job_is_not_picked_up_by_the_build_lane(store, project):
    """The predicate is not enough on its own: the claim query must actually skip it."""
    store.enqueue(project["id"], "serve", {"digest": "b" * 64},
                  allowed={project["state"]}, next_state="serving")
    assert store.claim("build") is None, "the build lane must not claim a serve job"
    claimed = store.claim("serve")
    assert claimed is not None and claimed["kind"] == "serve"


# -- HTTP contract -------------------------------------------------------------------

@pytest.fixture
def client(tmp_path):
    settings = Settings(data=tmp_path, operator_token="o" * 40, release_token="r" * 40,
                        pi="/usr/bin/true", model="test/model",
                        profiles={"dash": {"runner": "native", "commands": [["true"]], "artifact_dir": "index.html", "checks_dir": str(tmp_path)}})
    settings.validate()
    app = create_app(settings)
    with TestClient(app) as test_client:
        test_client.headers["Authorization"] = "Bearer " + settings.operator_token
        yield test_client, settings


def a_project(client, settings, artifact=None):
    created = client.post("/api/projects", json={
        "name": "Served Product", "brief": "A product whose build will be served locally.",
        "profile": "dash"}).json()
    if artifact:
        store = Store(settings.data / "factory.sqlite3")
        with store.tx() as db:
            db.execute("UPDATE projects SET artifact=?,state='ready' WHERE id=?",
                       (json.dumps(artifact), created["id"]))
    return created["id"]


def test_serving_without_an_artifact_is_refused(client):
    test_client, settings = client
    project = a_project(test_client, settings)
    response = test_client.post(f"/api/projects/{project}/serve")
    assert response.status_code == 422
    assert "verified artifact" in response.json()["detail"]


def test_serving_a_profile_without_serve_argv_is_refused_by_name(client):
    test_client, settings = client
    project = a_project(test_client, settings, artifact={"digest": "c" * 64, "job_id": "j1"})
    response = test_client.post(f"/api/projects/{project}/serve")
    assert response.status_code == 422
    assert "serve_argv" in response.json()["detail"]


def test_serve_enqueues_a_job_of_its_own_kind(client, tmp_path):
    test_client, settings = client
    settings.profiles["dash"] = servable_profile()
    project = a_project(test_client, settings, artifact={"digest": "d" * 64, "job_id": "j1"})
    response = test_client.post(f"/api/projects/{project}/serve")
    assert response.status_code == 202, response.text
    store = Store(settings.data / "factory.sqlite3")
    job = store.one("SELECT kind,payload FROM jobs WHERE project_id=? ORDER BY created DESC",
                    (project,))
    assert job["kind"] == "serve"
    assert json.loads(job["payload"])["digest"] == "d" * 64


def test_the_apps_endpoint_lists_a_port_for_the_tunnel(client):
    test_client, settings = client
    project = a_project(test_client, settings, artifact={"digest": "e" * 64, "job_id": "j1"})
    store = Store(settings.data / "factory.sqlite3")
    store.record_served(project, "j1", "e" * 64, 41234, "running", {"url": "http://127.0.0.1:41234"})
    body = test_client.get("/api/apps").json()
    assert body["apps"], "a running app must be listed"
    app = body["apps"][0]
    assert app["port"] == 41234
    assert "41234" in app["url"]


def test_stopping_an_app_that_is_not_served_is_a_404(client):
    test_client, settings = client
    project = a_project(test_client, settings)
    assert test_client.post(f"/api/projects/{project}/stop").status_code == 404


def test_stop_removes_the_record_so_the_lane_can_release_the_process(client):
    """The process lives in the serve lane; the API asks for a stop by removing the row."""
    test_client, settings = client
    project = a_project(test_client, settings, artifact={"digest": "f" * 64, "job_id": "j1"})
    store = Store(settings.data / "factory.sqlite3")
    store.record_served(project, "j1", "f" * 64, 41235, "running")
    assert test_client.post(f"/api/projects/{project}/stop").status_code == 200
    assert store.served(project) is None
    kinds = [e["kind"] for e in store.all("SELECT kind FROM events WHERE project_id=?", (project,))]
    assert "serve.stop_requested" in kinds


def test_serving_requires_the_operator_token(tmp_path):
    settings = Settings(data=tmp_path, operator_token="o" * 40, release_token="r" * 40,
                        pi="/usr/bin/true", model="test/model",
                        profiles={"dash": {"runner": "native", "commands": [["true"]], "artifact_dir": "index.html", "checks_dir": str(tmp_path)}})
    anonymous = TestClient(create_app(settings))
    assert anonymous.post("/api/projects/x/serve").status_code == 401
    assert anonymous.get("/api/apps").status_code == 401


# -- serve_argv translation ----------------------------------------------------------

def test_serve_argv_is_rewritten_relative_to_the_served_tree(tmp_path):
    """`serve_argv` is written against the repository, not against the build output.

    The profile says `node release/server/index.mjs` where `release` is `artifact_dir`, but the
    served tree *is* the contents of `release`. Running the argv as written looks for
    `release/release/server/index.mjs`, the process exits, and readiness times out. That is how
    the first live attempt failed.
    """
    tree = tmp_path / "served"
    (tree / "server").mkdir(parents=True)
    (tree / "server" / "index.mjs").write_text("// built\n")
    profile = servable_profile()
    assert serving.serve_argv_in(profile, tree) == ["node", "server/index.mjs"]


def test_an_argv_that_already_points_at_the_tree_is_left_alone(tmp_path):
    tree = tmp_path / "served"
    (tree / "server").mkdir(parents=True)
    (tree / "server" / "index.mjs").write_text("// built\n")
    profile = Profile.model_validate({"runner": "native", "commands": [["true"]],
                                      "artifact_dir": "release",
                                      "serve_argv": ["node", "server/index.mjs"]})
    assert serving.serve_argv_in(profile, tree) == ["node", "server/index.mjs"]


def test_the_rewrite_is_skipped_when_it_would_point_nowhere(tmp_path):
    """A wrong rewrite would hide the real failure behind a confusing path."""
    tree = tmp_path / "served"
    tree.mkdir()
    profile = Profile.model_validate({"runner": "native", "commands": [["true"]],
                                      "artifact_dir": "release",
                                      "serve_argv": ["node", "release/server/index.mjs"]})
    assert serving.serve_argv_in(profile, tree) == ["node", "release/server/index.mjs"], \
        "nothing resolves inside the tree, so the argv must be left as written"


def test_arguments_without_the_prefix_are_untouched(tmp_path):
    tree = tmp_path / "served"
    (tree / "server").mkdir(parents=True)
    (tree / "server" / "index.mjs").write_text("// built\n")
    profile = Profile.model_validate({"runner": "native", "commands": [["true"]],
                                      "artifact_dir": "release",
                                      "serve_argv": ["node", "--experimental", "server/index.mjs"]})
    assert "--experimental" in serving.serve_argv_in(profile, tree)
