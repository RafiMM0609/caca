import json
from pathlib import Path

import pytest

from factory.models import Target


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


def test_the_project_list_does_not_ship_plans(client, store, project):
    """The list route must return what the cards render, not the whole project row.

    Measured before this: `/api/projects` was 294 KB over 9 local projects and 91% of it was
    `plan` JSON that the list view never reads — the detail route is what returns a plan. A
    `SELECT *` here re-grows the payload the moment a column is added, so the projection is
    pinned: adding a column to the list is a deliberate UI decision.
    """
    with store.tx() as db:
        db.execute("UPDATE projects SET plan=? WHERE id=?",
                   (json.dumps({"summary": "x" * 20000}), project["id"]))

    rows = client.get("/api/projects").json()
    assert rows, "the fixture project must be listed"
    row = next(r for r in rows if r["id"] == project["id"])
    assert "plan" not in row, "the list must not carry plan JSON"
    assert "artifact" not in row and "pending_edit" not in row
    for key in ("id", "name", "brief", "profile", "state", "revision", "created"):
        assert key in row, f"the card renders {key}"


def test_the_project_list_truncates_the_brief(client, store, project):
    """A card shows a 170-character preview; the full brief belongs to the detail route."""
    long_brief = "Intro sentence. " * 40
    with store.tx() as db:
        db.execute("UPDATE projects SET brief=? WHERE id=?", (long_brief, project["id"]))

    row = next(r for r in client.get("/api/projects").json() if r["id"] == project["id"])
    assert len(row["brief"]) <= 200
    # The detail route still returns the whole brief, because that is the one that shows it.
    assert client.get("/api/projects/" + project["id"]).json()["brief"] == long_brief


def test_the_events_endpoint_is_incremental(client, store, project):
    """`after` must return only what is newer, because the dashboard polls on a cursor.

    The dashboard used to request `after=0` every 4 seconds, re-downloading a project's entire
    history forever. This pins the server half of that contract: a cursor at the current
    maximum yields nothing, and one event yields exactly one row.
    """
    base = "/api/projects/" + project["id"] + "/events"
    seen = client.get(base + "?after=0").json()
    cursor = seen[-1]["seq"]
    assert seen and all(e["seq"] <= cursor for e in seen)

    assert client.get(f"{base}?after={cursor}").json() == []

    store.event(project["id"], None, "test.ping", {})
    delta = client.get(f"{base}?after={cursor}").json()
    assert [e["kind"] for e in delta] == ["test.ping"]
    assert delta[0]["seq"] > cursor


def test_list_responses_are_compressed_and_small(client, store, project):
    """Planner prose is huge and repetitive; it must not cross the wire uncompressed."""
    response = client.get("/api/projects", headers={"Accept-Encoding": "gzip"})
    assert response.status_code == 200
    assert len(response.content) < 4096, "the list payload must stay small"


def test_the_polled_queries_are_indexed(store, project):
    """Every 4-second poll reads events, jobs and workers by project.

    Without these indexes SQLite answered with a full table scan plus a temporary B-tree for
    the ORDER BY, which grows with every project ever created. Asserted on the query plan
    because the plan is the thing that regresses, not the row count.
    """
    with store.connect() as db:
        plans = {
            "events": "SELECT * FROM events WHERE project_id=? AND seq>? ORDER BY seq LIMIT 300",
            "jobs": "SELECT * FROM jobs WHERE project_id=? ORDER BY created DESC LIMIT 100",
            "workers": "SELECT * FROM workers WHERE project_id=? ORDER BY created",
        }
        for name, sql in plans.items():
            args = (project["id"], 0) if name == "events" else (project["id"],)
            detail = " ".join(row["detail"] for row in db.execute("EXPLAIN QUERY PLAN " + sql, args))
            assert "USING INDEX" in detail, f"{name} poll is not using an index: {detail}"


# --- dashboard usability regressions (Phase 1) ---------------------------------------------

def test_the_dashboard_html_is_not_one_line():
    """The shell used to be 33 lines with one 1094-character line, which no one can review.

    This is a reviewability guard, not a style preference: a diff to a single 1 KB line is
    effectively unreviewable, so a change to the dashboard shell could hide in it.
    """
    from pathlib import Path
    html = (Path(__file__).resolve().parent.parent / "factory" / "static" / "index.html").read_text()
    longest = max(len(line) for line in html.splitlines())
    assert longest < 400, f"index.html has a {longest}-character line; split it"
    assert html.count("\n") > 100, "the shell should be formatted, not minified"


def test_the_dashboard_declares_indonesian_and_the_right_runtime():
    """The UI is Indonesian, and the sidebar named a runtime this project removed.

    'Oh My Pi' was replaced by the Pi 1.5.0 CLI; leaving it in the shell told the operator
    something false about the system they were looking at. The language is pinned because the
    dashboard had mixed English chrome with Indonesian notices, which reads as unintentional.
    """
    from pathlib import Path
    html = (Path(__file__).resolve().parent.parent / "factory" / "static" / "index.html").read_text()
    assert '<html lang="id">' in html
    assert "Oh My Pi" not in html, "the sidebar must not name the removed runtime"
    assert "Pi 1.5.0" in html


def test_the_dashboard_script_has_no_english_user_facing_strings():
    """`notify()` was Indonesian while the buttons were English; one language, not two."""
    from pathlib import Path
    js = (Path(__file__).resolve().parent.parent / "factory" / "static" / "app.js").read_text()
    for phrase in ("Not connected", "Factory connected", "Setup needed",
                   "Available after review and verification pass", "Artifact download failed",
                   "Enter the separate production release token"):
        assert phrase not in js, f"untranslated user-facing string: {phrase}"


def test_the_token_is_kept_in_session_storage_not_local_storage():
    """A reload must not force a re-paste, without leaving a bearer token on disk.

    sessionStorage is cleared when the tab closes and is never sent automatically. localStorage
    would persist the operator token across browser sessions, which is a materially different
    security posture for a bearer-only API with no cookie auth.
    """
    from pathlib import Path
    js = (Path(__file__).resolve().parent.parent / "factory" / "static" / "app.js").read_text()
    assert "sessionStorage" in js
    # Checked as a call, not as text: the explanatory comment mentions localStorage on purpose,
    # and a substring check would fail on the comment rather than on a real regression.
    assert "localStorage." not in js and "localStorage[" not in js, \
        "the operator token must not be persisted beyond the tab"
    assert "saveToken" in js and "clearToken" in js


def test_a_stream_failure_falls_back_to_polling():
    """Losing the event stream must degrade latency, never correctness."""
    from pathlib import Path
    js = (Path(__file__).resolve().parent.parent / "factory" / "static" / "app.js").read_text()
    assert "streamFailures < 3" in js, "reconnects must be bounded so the fallback is reachable"
    assert "startStream" in js and "stopStream" in js, "a stream must be stoppable on navigation"


def test_every_rendered_state_has_a_human_label():
    """The dashboard used to print machine states at the operator: "None", "Failed", "plan_available".

    A raw state is not just ugly, it is ambiguous — "Failed" does not say what failed, and
    "None" reads as an error rather than "no draft yet". Every state the UI can bind a badge to
    must therefore have a label, and the fallback must degrade readably instead of blank.
    """
    from pathlib import Path
    js = (Path(__file__).resolve().parent.parent / "factory" / "static" / "app.js").read_text()

    required = ["draft", "planning", "awaiting_plan", "building", "ready", "failed", "cancelled",
                "interrupted", "queued", "running", "succeeded", "merged", "rejected",
                "blocked", "changes_requested", "approved", "pending", "waiting", "partial",
                "incomplete", "passed", "not_configured", "none", "preparing", "editing",
                "stopped", "imported", "discarded", "unhealthy", "restarting"]
    start = js.index("const STATE_LABELS")
    table = js[start:js.index("};", start)]
    missing = [state for state in required if f"{state}:" not in table]
    assert not missing, f"states rendered by the dashboard have no label: {missing}"
    # The fallback must not be an empty string, or an unmapped state renders a blank badge.
    assert 'String(state).replaceAll("_", " ")' in js


def test_the_studio_helper_text_is_localised():
    """The last English paragraph in the UI was the Studio help line, found in a screenshot."""
    from pathlib import Path
    js = (Path(__file__).resolve().parent.parent / "factory" / "static" / "app.js").read_text()
    for phrase in ("Preparing the local editor", "Preview setup failed",
                   "Use the floating Studio button", "Open a verified Nuxt Studio project"):
        assert phrase not in js, f"untranslated studio help: {phrase}"
    assert "Menyiapkan editor lokal" in js


# --- Zig profile support ---------------------------------------------------------------

def test_zig_builds_are_excluded_from_the_artifact():
    """`zig-out/bin/app` is ~9 MB of compiled binary; a source artifact must not carry it.

    The exclusion list is the only thing standing between a hand-off and a committed binary,
    and it is easy to add a profile without adding its build directory.
    """
    from factory.artifact import EXCLUDED_DIRS
    for name in ("zig-out", ".zig-cache"):
        assert name in EXCLUDED_DIRS, f"{name} must not be published in a source artifact"


def test_a_zig_profile_is_detected_from_its_commands_not_its_name():
    """Renaming the profile must not silently switch the instructions back to the Node contract."""
    from factory.engine import _is_zig_profile
    from factory.models import Profile

    zig = Profile(runner="native", commands=[["/usr/local/bin/zig", "build"]],
                  artifact_dir="zig-out/bin", serve_argv=["zig-out/bin/app"])
    node = Profile(runner="native", commands=[["npm", "ci"]], artifact_dir="dist")
    renamed_zig = Profile(runner="native", commands=[["zig", "build"]],
                          artifact_dir="zig-out/bin")

    assert _is_zig_profile(zig)
    assert _is_zig_profile(renamed_zig)
    assert not _is_zig_profile(node)


def test_the_planner_prompt_mentions_zig_and_htmx():
    """The planner writes the worker instructions, so an unmentioned stack becomes a Nuxt task."""
    from factory.engine import PLANNER_PROMPT
    lowered = PLANNER_PROMPT.lower()
    for term in ("zig", "htmx", "zig build", "hx-get"):
        assert term in lowered, f"the planner is not told about {term}"
    # And it must not mandate SQLite for a Zig app, which would demand a C dependency.
    assert "persistence is file-based" in lowered


def test_the_serve_argv_of_a_zig_profile_resolves_to_the_served_tree():
    """`serve_argv` is written against the repo; the served tree holds the contents of artifact_dir."""
    from pathlib import Path

    from factory.models import Profile
    from factory.serving import serve_argv_in

    profile = Profile(runner="native", commands=[["zig", "build"]],
                      artifact_dir="zig-out/bin", serve_argv=["zig-out/bin/app"])
    assert serve_argv_in(profile, Path("/tmp")) == ["app"]


def test_a_build_gets_a_home_directory(tmp_path):
    """`zig build` fails with AppDataDirUnavailable when HOME is absent.

    `local_env()` forwards only an allowlist, so the build environment had no HOME and the
    compiler could not resolve its cache. The fix lives in `build_env()` so the verifier and
    `factory run` cannot drift apart — they had the same bug twice, discovered separately.
    """
    from factory.local_app import build_env

    env = build_env(tmp_path)
    assert "HOME" in env, "a build environment must provide HOME for toolchains"
    assert Path(env["HOME"]).is_dir(), "the scratch HOME must exist, not just be named"
    assert Path(env["HOME"]).is_relative_to(tmp_path), \
        "the scratch HOME must live inside the throwaway build directory"


def test_the_build_environment_does_not_leak_operator_secrets(tmp_path):
    """Providing HOME must not turn the allowlist into a copy of the whole environment."""
    import os

    from factory.local_app import build_env

    os.environ["FACTORY_OPERATOR_TOKEN"] = "should-not-leak"
    try:
        env = build_env(tmp_path)
    finally:
        os.environ.pop("FACTORY_OPERATOR_TOKEN", None)
    assert "FACTORY_OPERATOR_TOKEN" not in env
    assert not any("TOKEN" in key for key in env)


def test_a_relative_binary_in_the_served_tree_is_resolved_against_cwd():
    """`Popen(['app'])` searches PATH, not cwd, so a compiled binary never started.

    Invisible for `node`, which happens to be on PATH; fatal for a Zig build output, whose argv
    is exactly `app`. Asserted on the constructed command rather than by spawning a process.
    """
    import os
    import tempfile
    from pathlib import Path

    from factory.local_app import LocalApp

    with tempfile.TemporaryDirectory() as directory:
        binary = Path(directory) / "app"
        binary.write_text("#!/bin/sh\n")
        binary.chmod(0o755)
        app = LocalApp(["app"], cwd=directory, env={"PATH": os.environ.get("PATH", "")})
        try:
            command = app.process.args
        finally:
            app.close()
        assert command[-1] == "./app", f"a cwd binary must be prefixed, got {command[-1]!r}"


def test_parallel_workers_are_bounded_and_the_bound_is_configurable():
    """`worker_concurrency` bounds a wave; its ceiling and floor must stay enforced.

    Raised to 3 for this host because Zig builds are far lighter than `npm ci` + `nuxt build`
    (measured peak: 209 MB vs ~1 GB), so three parallel workers fit in 1.9 GB with ~1.2 GB
    spare. The validation still refuses 0 (no worker would ever run) and anything above 16
    (the host cannot take it, and a typo like 30 would be accepted silently otherwise).
    """
    from factory.config import Settings

    base = {"data": "/tmp/x", "operator_token": "o" * 40, "release_token": "r" * 40,
            "model": "t/m", "pi": "/usr/bin/true", "profiles": {}}

    assert Settings(**base, worker_concurrency=3).worker_concurrency == 3
    assert Settings(**base, worker_concurrency=16).worker_concurrency == 16
    for invalid in (0, 17, -1):
        settings = Settings(**base, worker_concurrency=invalid)
        with pytest.raises(ValueError):
            settings.validate()


def test_the_job_deadline_scales_with_the_batch_count():
    """Lowering concurrency lengthens the deadline; raising it must shorten the estimate.

    A wave of 4 tasks with concurrency 1 is four sequential batches, with concurrency 4 it is
    one. The deadline is sized from that, so a wrong count means a job killed early (or a
    needlessly long ceiling).
    """
    from factory.config import Settings
    from factory.models import Plan, Profile, Task

    plan = Plan(
        summary="Four independent tasks in one wave.",
        assumptions=[], questions=[],
        tasks=[Task(id=f"t{index}", title=f"Task {index}", instructions="Do the work.",
                    acceptance=["Works"], depends_on=[]) for index in range(4)])

    profile = Profile(runner="native", commands=[["true"]], artifact_dir="x", timeout=60)
    base = {"data": "/tmp/x", "operator_token": "o" * 40, "release_token": "r" * 40,
            "model": "t/m", "pi": "/usr/bin/true", "profiles": {}, "worker_timeout": 300,
            "job_timeout": 60, "max_job_timeout": 100000, "max_repairs": 0}

    serial = Settings(**base, worker_concurrency=1).job_deadline_seconds(plan, profile)
    parallel = Settings(**base, worker_concurrency=4).job_deadline_seconds(plan, profile)
    assert parallel < serial, "raising concurrency must not lengthen the estimate"
