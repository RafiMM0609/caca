import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from factory.db import Conflict
from factory.engine import Engine
from factory.models import Change, Review


class ScriptedAgent:
    """Deterministic test double, NEVER a production provider or reported as an AI test."""
    def __init__(self, plan, reject=0):
        self.plan, self.reject = plan, reject
        self.calls = []

    def generate(self, role, instruction, context, schema, cancelled):
        self.calls.append(role)
        if role == "planner":
            return self.plan
        if role == "implementer":
            return Change(summary="Create portfolio", files={"index.html": "<!doctype html><html lang='en'><head><title>Portfolio</title></head><body><h1>Ada</h1><p>ada@example.test</p></body></html>"})
        if self.reject:
            self.reject -= 1
            return Review(approved=False, findings=["Fix accessibility"])
        return Review(approved=True, findings=[])


class TestVerifier:
    __test__ = False

    def verify(self, profile, source, output, job, cancelled):
        output.mkdir()
        (output / "index.html").write_text((source / "index.html").read_text())
        return "TEST DOUBLE: not a container verification"


def build_job(store, project):
    return store.enqueue(project["id"], "build", {"revision": project["revision"]},
                         allowed={"awaiting_plan"}, next_state="building")


def test_concurrent_claim_is_exactly_once(store, project):
    queued = build_job(store, project)
    with ThreadPoolExecutor(max_workers=8) as pool:
        claims = list(pool.map(lambda _: store.claim(), range(8)))
    assert [c["id"] for c in claims if c] == [queued["id"]]
    with pytest.raises(Conflict):
        build_job(store, project)


def test_stale_and_unresolved_plans_rejected(store, project, plan):
    with pytest.raises(Conflict, match="revision"):
        store.enqueue(project["id"], "build", {"revision": 0})
    plan.questions = ["What payment provider is required?"]
    with store.tx() as db:
        db.execute("UPDATE projects SET plan=? WHERE id=?", (plan.model_dump_json(), project["id"]))
    with pytest.raises(Conflict, match="questions"):
        build_job(store, project)


def test_recovery_never_requeues_side_effects(store, project):
    build_job(store, project)
    job = store.claim()
    store.recover()
    assert store.one("SELECT * FROM jobs WHERE id=?", (job["id"],))["state"] == "interrupted"
    assert store.claim() is None
    assert store.project(project["id"])["state"] == "interrupted"


def test_queued_cancel_releases_project_slot(store, project):
    job = build_job(store, project)
    store.cancel(job["id"])
    assert store.claim() is None
    assert store.project(project["id"])["state"] == "cancelled"


def test_build_repairs_then_publishes_evidence(settings, store, project, plan):
    agent = ScriptedAgent(plan, reject=1)
    build_job(store, project)
    job = store.claim()
    Engine(settings, store, agent, TestVerifier()).process(job)
    result = store.project(project["id"])
    assert result["state"] == "ready"
    artifact = json.loads(result["artifact"])
    assert len(artifact["digest"]) == 64
    assert (settings.data / "jobs" / job["id"] / "artifact.tar.gz").is_file()
    assert agent.calls == ["implementer", "review", "implementer", "review"]
    assert store.one("SELECT * FROM jobs WHERE id=?", (job["id"],))["reserved_usd"] == 8


def test_review_failure_never_produces_artifact(settings, store, project, plan):
    build_job(store, project)
    job = store.claim()
    Engine(settings, store, ScriptedAgent(plan, reject=10), TestVerifier()).process(job)
    assert store.project(project["id"])["artifact"] is None
    assert store.project(project["id"])["state"] == "failed"


def test_budget_stops_new_model_calls(settings, store, project, plan):
    settings.max_job_cost = 2
    agent = ScriptedAgent(plan)
    build_job(store, project)
    Engine(settings, store, agent, TestVerifier()).process(store.claim())
    assert agent.calls == ["implementer"]
    assert store.project(project["id"])["artifact"] is None


def test_verifier_failure_never_produces_release(settings, store, project, plan):
    class FailedVerifier:
        def verify(self, *args):
            raise RuntimeError("test failure")
    build_job(store, project)
    Engine(settings, store, ScriptedAgent(plan), FailedVerifier()).process(store.claim())
    assert store.project(project["id"])["state"] == "failed"
    assert store.project(project["id"])["artifact"] is None


def test_planner_requires_human_approval(settings, store, project, plan):
    store.enqueue(project["id"], "plan", next_state="planning")
    Engine(settings, store, ScriptedAgent(plan), TestVerifier()).process(store.claim())
    result = store.project(project["id"])
    assert result["state"] == "awaiting_plan"
    assert result["revision"] == 2
    assert store.claim() is None


def test_sqlite_online_backup(store, project, tmp_path):
    from factory.db import Store
    path = tmp_path / "snapshot.sqlite3"
    store.backup(path)
    assert Store(path).project(project["id"])["plan"] == project["plan"]

