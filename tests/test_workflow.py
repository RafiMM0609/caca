import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from factory.db import Conflict
from factory.engine import Engine
from factory.models import Review


class ScriptedAgent:
    """Deterministic test double, NEVER a production provider or reported as an AI test.

    ``generate`` backs the planner (and the reviewer); implementation goes through
    ``ScriptedWorker`` because a worker edits its own worktree instead of returning
    whole-file JSON.
    """
    def __init__(self, plan, reject=0):
        self.plan, self.reject = plan, reject
        self.calls = []

    def generate(self, role, instruction, context, schema, cancelled):
        self.calls.append(role)
        if role == "planner":
            return self.plan
        if self.reject:
            self.reject -= 1
            return Review(approved=False, findings=["Fix accessibility"])
        return Review(approved=True, findings=[])


class ScriptedWorker:
    """Stand-in for the Pi harness: writes real files into its worktree.

    The engine no longer asks a model for whole-file JSON; a worker edits its tree, so
    the double must edit the tree too. Still a test double, never a live model.
    """
    def __init__(self, files=None):
        self.files = files or {"index.html": "<!doctype html><html lang='en'><head><title>Portfolio</title></head>"
                                           "<body><h1>Ada</h1><p>ada@example.test</p></body></html>"}
        self.calls = 0

    def run_task(self, worktree, instruction, model="", timeout=0, cancelled=lambda: False):
        self.calls += 1
        for name, content in self.files.items():
            target = worktree / name
            target.parent.mkdir(parents=True, exist_ok=True)
            # Unique per call so a second worker is not silently a no-op change.
            target.write_text(content + f"<!-- worker call {self.calls} -->\n")
        return "scripted worker transcript"


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
    Engine(settings, store, agent, TestVerifier(), worker=ScriptedWorker()).process(job)
    workers = store.workers_for(job["id"])
    result = store.project(project["id"])
    assert result["state"] == "ready"
    artifact = json.loads(result["artifact"])
    assert len(artifact["digest"]) == 64
    assert (settings.data / "jobs" / job["id"] / "artifact.tar.gz").is_file()
    # One worker ran per repair attempt, and each attempt was reviewed.
    assert agent.calls.count("review") == 2
    assert workers[0]["state"] == "merged"
    # Two reviewer calls at $2 each. Workers are harness processes, not budgeted JSON calls.
    assert store.one("SELECT * FROM jobs WHERE id=?", (job["id"],))["reserved_usd"] == 4


def test_review_failure_never_produces_artifact(settings, store, project, plan):
    build_job(store, project)
    job = store.claim()
    Engine(settings, store, ScriptedAgent(plan, reject=10), TestVerifier(), worker=ScriptedWorker()).process(job)
    assert store.project(project["id"])["artifact"] is None
    assert store.project(project["id"])["state"] == "failed"


def test_budget_stops_reviews_and_blocks_publication(settings, store, project, plan):
    """Two tasks need two reviews; a $2 job budget only pays for one.

    The second review cannot be reserved, so the wave fails and nothing is published.
    """
    settings.max_job_cost = 2
    from factory.models import Task
    plan.tasks = [
        Task(id="one", title="First part", instructions="Build the first part of the site.",
             acceptance=["works"], depends_on=[]),
        Task(id="two", title="Second part", instructions="Build the second part of the site.",
             acceptance=["works"], depends_on=["one"]),
    ]
    with store.tx() as db:
        db.execute("UPDATE projects SET plan=? WHERE id=?", (plan.model_dump_json(), project["id"]))
    agent = ScriptedAgent(plan)
    build_job(store, project)
    Engine(settings, store, agent, TestVerifier(), worker=ScriptedWorker()).process(store.claim())
    assert agent.calls.count("review") == 1, "only one review fits the budget"
    assert store.project(project["id"])["artifact"] is None
    assert store.project(project["id"])["state"] == "failed"


def test_verifier_failure_never_produces_release(settings, store, project, plan):
    class FailedVerifier:
        def verify(self, *args):
            raise RuntimeError("test failure")
    build_job(store, project)
    Engine(settings, store, ScriptedAgent(plan), FailedVerifier(), worker=ScriptedWorker()).process(store.claim())
    assert store.project(project["id"])["state"] == "failed"
    assert store.project(project["id"])["artifact"] is None


def test_planner_requires_human_approval(settings, store, project, plan):
    store.enqueue(project["id"], "plan", next_state="planning")
    Engine(settings, store, ScriptedAgent(plan), TestVerifier(), worker=ScriptedWorker()).process(store.claim())
    result = store.project(project["id"])
    assert result["state"] == "awaiting_plan"
    assert result["revision"] == 2
    assert store.claim() is None


def test_sqlite_online_backup(store, project, tmp_path):
    from factory.db import Store
    path = tmp_path / "snapshot.sqlite3"
    store.backup(path)
    assert Store(path).project(project["id"])["plan"] == project["plan"]



def test_recovery_reconciles_workers_left_running(store, project, plan):
    """A worker left 'running' by a dead scheduler must not stay running forever.

    The dashboard reads the workers table; a stale 'running' row would show work in
    progress for a job that already ended, and nothing ever cleared it.
    """
    build_job(store, project)
    claimed = store.claim()
    running = store.create_worker(project["id"], claimed["id"], "task-a", "worker/task-a", "base")
    store.set_worker(running["id"], state="running")
    done = store.create_worker(project["id"], claimed["id"], "task-b", "worker/task-b", "base")
    store.set_worker(done["id"], state="merged")

    store.recover()

    states = {row["task_id"]: row["state"] for row in store.workers_for(claimed["id"])}
    assert states["task-a"] == "interrupted", "a running worker must be reconciled"
    assert states["task-b"] == "merged", "a finished worker must keep its outcome"


def test_recovery_reconciles_workers_of_already_finished_jobs(store, project, plan):
    """A worker can be left running after its job already ended.

    Recovering only jobs that were running at restart left an old row 'running' forever,
    which made the dashboard show work in progress for a project that is ready.
    """
    build_job(store, project)
    claimed = store.claim()
    stale = store.create_worker(project["id"], claimed["id"], "task-a", "worker/task-a", "base")
    store.set_worker(stale["id"], state="running")
    store.finish(claimed["id"], "succeeded", {})

    store.recover()

    states = {row["task_id"]: row["state"] for row in store.workers_for(claimed["id"])}
    assert states["task-a"] == "interrupted"


class FlakyPlanner:
    """Fails the planner N times, then returns a valid plan. Mirrors a real provider."""

    def __init__(self, plan, fail_times):
        self.plan, self.fail_times, self.calls = plan, fail_times, 0

    def generate(self, role, instruction, context, schema, cancelled):
        if role == "planner":
            self.calls += 1
            if self.calls <= self.fail_times:
                error = ValueError("Model returned no text")
                error.output = "{}"
                raise error
            return self.plan
        if role == "review":
            return Review(approved=True, findings=[])
        return self.plan


def test_planner_retries_a_malformed_response(settings, store, project, plan):
    """One bad generation must not end the run; the tester has the same retry."""
    from factory.engine import Engine
    agent = FlakyPlanner(plan, fail_times=1)
    job = store.enqueue(project["id"], "plan", next_state="planning")
    Engine(settings, store, agent, TestVerifier()).process(store.claim())
    assert agent.calls == 2, "the planner must retry once before giving up"
    assert store.one("SELECT state FROM jobs WHERE id=?", (job["id"],))["state"] == "succeeded"
    kinds = [row["kind"] for row in store.all("SELECT kind FROM events WHERE job_id=?", (job["id"],))]
    assert "plan.attempt_failed" in kinds, "a failed attempt must be recorded as evidence"


def test_planner_gives_up_after_the_bounded_retries(settings, store, project, plan):
    from factory.engine import Engine
    agent = FlakyPlanner(plan, fail_times=99)
    job = store.enqueue(project["id"], "plan", next_state="planning")
    Engine(settings, store, agent, TestVerifier()).process(store.claim())
    assert agent.calls == settings.max_repairs + 1, "retries are bounded, not infinite"
    assert store.one("SELECT state FROM jobs WHERE id=?", (job["id"],))["state"] == "failed"
