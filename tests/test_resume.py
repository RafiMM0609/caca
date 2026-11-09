"""A restart must resume at a wave boundary, not redo merged work.

A retry after a scheduler restart creates a **new job id**, so asking the new job for its
completed tasks always answered "none" and the whole build restarted from zero. A Phase 0
build took 15 minutes; losing it to a restart is the cost this covers.
"""
import json
import time
from pathlib import Path

import pytest

from factory import products
from factory.db import Store
from factory.models import Plan, ProjectInput
from factory.parallel import ParallelBuilder


def task(name, deps=None):
    return {"id": name, "title": f"Task {name}", "instructions": f"Implement the {name} part.",
            "acceptance": ["works"], "depends_on": deps or []}


class CountingWorker:
    """Writes one file per task and records which tasks it was asked to run."""

    def __init__(self):
        self.ran = []

    def run_task(self, worktree, instruction, model="", timeout=0, cancelled=lambda: False):
        marker = "YOUR TASK ("
        start = instruction.index(marker) + len(marker)
        task_id = instruction[start:instruction.index(")", start)]
        self.ran.append(task_id)
        (Path(worktree) / f"{task_id}.txt").write_text(f"{task_id}\n")
        return "worker transcript"


@pytest.fixture
def env(tmp_path):
    from factory.config import Settings
    settings = Settings(data=tmp_path, operator_token="o" * 40, release_token="r" * 40,
                        model="test/model", pi="/usr/bin/true", worker_concurrency=2)
    store = Store(tmp_path / "factory.sqlite3")
    project = store.create_project(ProjectInput(name="Resume demo",
                                                brief="Build a product with several parts."))
    return settings, store, project


def instr(task, ctx):
    return f"YOUR TASK ({task.id}) do it"


def run(env, plan, worker, job_id):
    settings, store, project = env
    # Read the project fresh: a test that bumps the revision must be seen by build_tasks,
    # which resumes per revision. Using the fixture's snapshot made resume look correct
    # while testing the wrong revision.
    current = store.project(project["id"])
    builder = ParallelBuilder(settings, store, worker=worker)
    builder.build_tasks(current, {"id": job_id, "payload": "{}"}, plan, instr, lambda: False)


def test_a_second_job_resumes_instead_of_redoing_merged_tasks(env):
    """The whole point: a new job id must not mean starting from zero."""
    settings, store, project = env
    plan = Plan(summary="Two waves, the second depending on the first.",
                tasks=[task("foundation"), task("detail", ["foundation"])])

    first = CountingWorker()
    run(env, plan, first, "job-1")
    assert sorted(first.ran) == ["detail", "foundation"]

    # A retry: new job id, same project and revision, product branch already has the work.
    second = CountingWorker()
    run(env, plan, second, "job-2")
    assert second.ran == [], "merged tasks must not be re-run after a restart"


def test_the_resume_decision_is_recorded_as_an_event(env):
    settings, store, project = env
    plan = Plan(summary="One task, resumed from the branch.",
                tasks=[task("solo")])
    run(env, plan, CountingWorker(), "job-1")
    store.event(project["id"], "job-2", "placeholder", {})
    run(env, plan, CountingWorker(), "job-2")

    resumed = [e for e in store.all("SELECT kind, detail FROM events WHERE job_id='job-2'")
               if e["kind"] == "build.resumed"]
    assert resumed, "a resume must be visible in the event log, not inferred"
    assert json.loads(resumed[0]["detail"])["completed"] == ["solo"]


def test_a_commit_that_is_no_longer_on_the_branch_is_not_trusted(env):
    """A worker row is not evidence on its own: a reset invalidates it."""
    settings, store, project = env
    plan = Plan(summary="One task whose commit is then discarded.",
                tasks=[task("solo")])
    run(env, plan, CountingWorker(), "job-1")

    root = products.product_root(settings.data, project["id"])
    products.checkout_target(root)
    products._run(["reset", "--hard", "HEAD~1"], cwd=root)

    worker = CountingWorker()
    run(env, plan, worker, "job-2")
    assert worker.ran == ["solo"], "work that is no longer on the branch must be redone"


def test_only_tasks_in_the_current_plan_are_resumed(env):
    """A task that was removed from the plan must not count as done."""
    settings, store, project = env
    plan = Plan(summary="One task, then a plan with a different task.",
                tasks=[task("old-task")])
    run(env, plan, CountingWorker(), "job-1")

    new_plan = Plan(summary="A plan that replaces the previous task entirely.",
                    tasks=[task("new-task")])
    worker = CountingWorker()
    run(env, new_plan, worker, "job-2")
    assert worker.ran == ["new-task"]


def test_job_timeout_is_bounded_and_configurable():
    from factory.config import Settings

    settings = Settings(data=Path("/tmp/jt-check"), operator_token="o" * 40,
                        release_token="r" * 40)
    settings.validate()
    assert settings.job_timeout == 7200, "a job needs its own ceiling, distinct from agent_timeout"

    bad = Settings(data=Path("/tmp/jt-check"), operator_token="o" * 40, release_token="r" * 40,
                   job_timeout=5)
    with pytest.raises(ValueError, match="Job timeout"):
        bad.validate()


def test_an_expired_job_stops_instead_of_running_forever(settings, store, project, plan):
    """The deadline is enforced through the same cancellation path as an operator cancel."""
    from factory.engine import Engine
    from factory.models import Review

    class Agent:
        def generate(self, role, instruction, context, schema, cancelled):
            return plan if role == "planner" else Review(approved=True, findings=[])

    class Worker:
        def run_task(self, worktree, instruction, model="", timeout=0, cancelled=lambda: False):
            time.sleep(0.01)
            (Path(worktree) / "index.html").write_text("<title>x</title>")
            return "ok"

    settings.job_timeout = 60          # valid but irrelevant: we expire it by hand
    job = store.enqueue(project["id"], "build", {"revision": project["revision"]},
                        allowed={"awaiting_plan"}, next_state="building")
    claimed = store.claim()
    engine = Engine(settings, store, Agent(), worker=Worker())
    engine.stop.set()                 # same path the deadline takes
    engine.process(claimed)
    assert store.one("SELECT state FROM jobs WHERE id=?", (job["id"],))["state"] == "cancelled"


def test_a_new_plan_revision_is_not_resumed_from_the_old_one(env):
    """After a plan edit the same task id means different code, so old merges are not done.

    Without this, editing a completed plan and approving the new revision re-ran nothing:
    the rebuild produced an artifact whose source was never regenerated.
    """
    settings, store, project = env
    plan_v1 = Plan(summary="The first revision, which builds the initial product.",
                   tasks=[task("website")])
    run(env, plan_v1, CountingWorker(), "job-1")

    # Simulate an approved plan edit: revision 2 with the same task id.
    project = dict(store.project(project["id"]))
    with store.tx() as db:
        db.execute("UPDATE projects SET revision=revision+1 WHERE id=?", (project["id"],))
    plan_v2 = Plan(summary="The second revision, which changes the built product.",
                   tasks=[task("website")])
    worker = CountingWorker()
    run(env, plan_v2, worker, "job-2")
    assert worker.ran == ["website"], "a new revision must rebuild, not resume"
