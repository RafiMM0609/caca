"""The job ceiling scales with the plan, and the run records where its time went.

A flat `job_timeout` is wrong in both directions: too small and a legitimate larger plan dies
with no defect to find, too large and a wedged job wastes hours. Waves drive the cost, because
tasks inside a wave run concurrently.
"""
import json
import threading
import time
from pathlib import Path

import pytest

from factory.config import Settings
from factory.models import Plan, Profile, ProjectInput, Review


def chain(n):
    return Plan.model_validate({
        "summary": "A chained plan used to check how the budget scales with waves.",
        "tasks": [{"id": f"t{i}", "title": f"Task number {i}",
                   "instructions": "Do this piece of work.",
                   "acceptance": ["works"],
                   "depends_on": [f"t{i-1}"] if i else []} for i in range(n)],
        "test_flows": []})


def flat(n):
    return Plan.model_validate({
        "summary": "A parallel plan used to check that width does not inflate the budget.",
        "tasks": [{"id": f"t{i}", "title": f"Task number {i}",
                   "instructions": "Do this piece of work.",
                   "acceptance": ["works"], "depends_on": []} for i in range(n)],
        "test_flows": []})


@pytest.fixture
def settings(tmp_path):
    """Settings with the profile the project fixture refers to, or Engine cannot build."""
    return Settings(data=tmp_path, operator_token="o" * 40, release_token="r" * 40,
                    model="test/model", pi="/usr/bin/true",
                    profiles={"static": Profile(runner="native", commands=[["true"]],
                                                timeout=900)})


def profile_with(timeout=900):
    return Profile(runner="native", commands=[["true"]], timeout=timeout)


def test_waves_matches_how_the_builder_schedules(settings):
    plan = chain(4)
    assert plan.waves() == [["t0"], ["t1"], ["t2"], ["t3"]]
    assert flat(4).waves() == [["t0", "t1", "t2", "t3"]]


def test_the_budget_grows_with_waves_not_tasks(settings):
    small = settings.job_deadline_seconds(chain(2), profile_with())
    large = settings.job_deadline_seconds(chain(8), profile_with())
    assert large > small, "a longer chain needs more wall time"

    wide = settings.job_deadline_seconds(flat(8), profile_with())
    assert wide < large, "eight concurrent tasks are one wave, eight chained ones are eight"


def test_the_configured_timeout_is_a_floor(settings):
    """An operator who raised `job_timeout` still gets their number for a small plan."""
    settings.job_timeout = 20000
    settings.max_job_timeout = 40000
    assert settings.job_deadline_seconds(chain(1), profile_with()) == 20000


def test_the_estimate_is_capped_so_a_huge_plan_cannot_run_forever(settings):
    """The largest plan the schema allows still cannot claim an unbounded run."""
    enormous = settings.job_deadline_seconds(chain(30), profile_with())
    assert enormous == settings.max_job_timeout


def test_a_plan_only_job_uses_the_flat_value(settings):
    assert settings.job_deadline_seconds(None, None) == settings.job_timeout


def test_concurrency_lowers_the_cost_of_a_wide_wave(settings):
    settings.worker_concurrency = 4
    four_wide = settings.job_deadline_seconds(flat(4), profile_with())
    settings.worker_concurrency = 1
    one_at_a_time = settings.job_deadline_seconds(flat(4), profile_with())
    assert one_at_a_time > four_wide, "a wave wider than the concurrency needs extra batches"


def test_an_inconsistent_timeout_configuration_is_rejected(tmp_path):
    bad = Settings(data=tmp_path, operator_token="o" * 40, release_token="r" * 40,
                   job_timeout=5000, max_job_timeout=1000)
    with pytest.raises(ValueError, match="Max job timeout"):
        bad.validate()


def test_a_run_records_where_its_time_went(settings, store, project, plan):
    """Without a breakdown the operator cannot know which number to raise."""
    from factory.engine import Engine

    class Agent:
        def generate(self, role, instruction, context, schema, cancelled):
            return plan if role == "planner" else Review(approved=True, findings=[])

    class Worker:
        def run_task(self, worktree, instruction, model="", timeout=0, cancelled=lambda: False,
                 skills=()):
            (Path(worktree) / "index.html").write_text("<title>x</title>")
            return "ok"

    class Verifier:
        __test__ = False

        def verify(self, profile, source, output, job, cancelled, **testing):
            output.mkdir(parents=True, exist_ok=True)
            (output / "index.html").write_text("<title>x</title>")
            return "passed"

    job = store.enqueue(project["id"], "build", {"revision": project["revision"]},
                        allowed={"awaiting_plan"}, next_state="building")
    Engine(settings, store, Agent(), Verifier(), worker=Worker()).process(store.claim())

    events = {e["kind"]: e["detail"] for e in
              store.all("SELECT kind, detail FROM events WHERE job_id=?", (job["id"],))}
    assert "job.budget" in events, "the budget the job was given must be visible"
    assert "job.timing" in events, "the post-mortem must be recorded"

    budget = json.loads(events["job.budget"])
    assert budget["seconds"] >= settings.job_timeout

    timing = json.loads(events["job.timing"])
    for key in ("total_seconds", "budget_seconds", "used_fraction", "waves",
                "worker_seconds", "verification_seconds"):
        assert key in timing, f"{key} is part of the breakdown"
    assert timing["waves"], "each wave records its duration"
    assert timing["worker_seconds"], "each worker records how long it ran"
    assert 0 <= timing["used_fraction"] <= 1


def test_worker_durations_survive_into_the_worker_row(settings, store, project, plan):
    from factory.engine import Engine

    class Agent:
        def generate(self, role, instruction, context, schema, cancelled):
            return plan if role == "planner" else Review(approved=True, findings=[])

    class Worker:
        def run_task(self, worktree, instruction, model="", timeout=0, cancelled=lambda: False,
                 skills=()):
            (Path(worktree) / "index.html").write_text("<title>x</title>")
            return "ok"

    class Verifier:
        __test__ = False

        def verify(self, profile, source, output, job, cancelled, **testing):
            output.mkdir(parents=True, exist_ok=True)
            (output / "index.html").write_text("<title>x</title>")
            return "passed"

    store.enqueue(project["id"], "build", {"revision": project["revision"]},
                  allowed={"awaiting_plan"}, next_state="building")
    Engine(settings, store, Agent(), Verifier(), worker=Worker()).process(store.claim())

    rows = store.workers_for(store.one(
        "SELECT id FROM jobs WHERE kind='build'")["id"])
    assert rows, "a worker row is written"
    detail = json.loads(rows[0]["detail"] or "{}")
    assert "seconds" in detail, "a worker's duration is part of its evidence"


# --- worker-run cost metering ---------------------------------------------------------
# Harness workers were the one unmetered model spend. These tests keep the reservation
# transactional: `ParallelBuilder` runs workers in threads, so a racy reservation could
# silently exceed the job ceiling the operator set to survive on a small bill.


def build_job(store, project):
    return store.enqueue(project["id"], "build", {"revision": project["revision"]},
                         allowed={"awaiting_plan"}, next_state="building")


class FileWorker:
    """A scripted harness worker: writes one file per task, records what it ran."""

    def __init__(self, sleep=0.0):
        self.sleep = sleep
        self.ran = []
        self.lock = threading.Lock()

    def run_task(self, worktree, instruction, model="", timeout=0, cancelled=lambda: False,
                 skills=()):
        marker = "YOUR TASK ("
        task_id = instruction[instruction.index(marker) + len(marker):instruction.index(
            ")", instruction.index(marker))]
        with self.lock:
            self.ran.append(task_id)
        (Path(worktree) / f"{task_id}.txt").write_text(f"{task_id}\n")
        time.sleep(self.sleep)
        return "worker transcript"


def flat_plan(n):
    return Plan(summary="Independent parts.", assumptions=[], questions=[], tasks=[
        {"id": f"t{i}", "title": f"Part {i}", "instructions": "Do this piece of work.",
         "acceptance": ["works"], "depends_on": []} for i in range(n)], test_flows=[])


def reserved_usd(store, job_id):
    return store.one("SELECT reserved_usd FROM jobs WHERE id=?", (job_id,))["reserved_usd"]


def test_a_worker_run_reserves_its_cost_against_the_job(settings, store, project):
    """The reservation is visible in the same `jobs.reserved_usd` ledger the reviews use."""
    from factory.parallel import ParallelBuilder

    settings.worker_cost = 0.5
    settings.max_job_cost = 10
    job = build_job(store, project)
    builder = ParallelBuilder(settings, store, worker=FileWorker())
    builder.build_tasks(project, job, flat_plan(2),
                        lambda task, ctx: f"YOUR TASK ({task.id}) do it", lambda: False)

    assert reserved_usd(store, job["id"]) == 1.0, "two workers at $0.50 each are metered"
    rows = store.workers_for(job["id"])
    assert len(rows) == 2
    assert all(row["state"] == "merged" for row in rows)


def test_concurrent_reservations_never_exceed_the_job_ceiling(settings, store, project):
    """Four workers race on a budget that only pays for three; one must fail loudly.

    The builder runs a wave in threads exactly as production does, so this exercises the
    real `BEGIN IMMEDIATE` serialization rather than a mocked one.
    """
    from factory.parallel import ParallelBuilder

    settings.worker_cost = 1.0
    settings.max_job_cost = 3.0
    settings.worker_concurrency = 4
    job = build_job(store, project)
    worker = FileWorker(sleep=0.1)
    builder = ParallelBuilder(settings, store, worker=worker)
    with pytest.raises(ValueError, match="failed after bounded repair attempts"):
        builder.build_tasks(project, job, flat_plan(4),
                            lambda task, ctx: f"YOUR TASK ({task.id}) do it", lambda: False)

    spent = reserved_usd(store, job["id"])
    assert spent <= settings.max_job_cost, "metering must be exact under concurrency"
    assert spent == 3.0, "three runs at $1.00 fit; nothing above the ceiling was charged"

    failed = [row for row in store.workers_for(job["id"]) if row["state"] == "failed"]
    assert len(failed) == 1, "exactly one run was refused"
    detail = json.loads(failed[0]["detail"] or "{}")
    assert "FACTORY_WORKER_COST" in detail.get("message", ""), \
        "the error names the knob the operator has to raise"
    assert failed[0]["task_id"] not in worker.ran, \
        "a refused run must never start model work"
    assert all(row["state"] != "running" for row in store.workers_for(job["id"])), \
        "no worker may be left looking alive after its budget was refused"

    kinds = [e["kind"] for e in store.all("SELECT kind FROM events WHERE job_id=?", (job["id"],))]
    assert kinds.count("worker.budget_exhausted") >= 1, "the refusal is visible on the dashboard"


def test_worker_cost_zero_disables_metering(settings, store, project):
    """`FACTORY_WORKER_COST=0` opts out and restores today's unmetered behaviour."""
    from factory.parallel import ParallelBuilder

    settings.worker_cost = 0.0
    settings.max_job_cost = 0.01
    job = build_job(store, project)
    builder = ParallelBuilder(settings, store, worker=FileWorker())
    builder.build_tasks(project, job, flat_plan(2),
                        lambda task, ctx: f"YOUR TASK ({task.id}) do it", lambda: False)

    assert reserved_usd(store, job["id"]) == 0, "no reservation is charged"
    rows = store.workers_for(job["id"])
    assert len(rows) == 2 and all(row["state"] == "merged" for row in rows), \
        "workers run even when the ceiling is far below their real cost"


def test_the_worker_cost_knob_reads_the_environment(tmp_path, monkeypatch):
    monkeypatch.delenv("FACTORY_WORKER_COST", raising=False)
    assert Settings.from_env().worker_cost == 0.25, "the documented default"

    monkeypatch.setenv("FACTORY_WORKER_COST", "0.75")
    assert Settings.from_env().worker_cost == 0.75


def test_a_negative_worker_cost_is_rejected(tmp_path):
    bad = Settings(data=tmp_path, operator_token="o" * 40, release_token="r" * 40,
                   worker_cost=-0.01)
    with pytest.raises(ValueError, match="Worker cost"):
        bad.validate()


def test_a_worker_cost_above_the_job_ceiling_is_rejected(tmp_path):
    bad = Settings(data=tmp_path, operator_token="o" * 40, release_token="r" * 40,
                   worker_cost=5.0, max_job_cost=2.0)
    with pytest.raises(ValueError, match="at least one harness worker run"):
        bad.validate()


def test_usage_is_recorded_only_when_the_transcript_carries_it(settings, store, project):
    """Scavenging usage from the transcript must never fail a worker."""
    from factory.agents import usage_from_transcript
    from factory.parallel import ParallelBuilder

    class MeteredWorker(FileWorker):
        def run_task(self, worktree, instruction, **kwargs):
            super().run_task(worktree, instruction, **kwargs)
            return json.dumps({"type": "message_end", "message": {"role": "assistant"},
                               "usage": {"input": 1200, "output": 300,
                                         "cost": {"total": 0.012}}}) + "\n"

    class SilentWorker(FileWorker):
        def run_task(self, worktree, instruction, **kwargs):
            super().run_task(worktree, instruction, **kwargs)
            return "plain text transcript, no json at all"

    settings.worker_cost = 0.0
    job = build_job(store, project)
    ParallelBuilder(settings, store, worker=MeteredWorker()).build_tasks(
        project, job, flat_plan(1), lambda task, ctx: f"YOUR TASK ({task.id}) do it",
        lambda: False)
    usage = [e for e in store.all("SELECT kind, detail FROM events WHERE job_id=? AND kind='worker.usage'",
                                  (job["id"],))]
    assert len(usage) == 1, "metering data the harness reported is recorded"
    detail = json.loads(usage[0]["detail"])
    assert detail["tokens"] == {"input": 1200, "output": 300}
    assert detail["cost_usd"] == pytest.approx(0.012)

    assert usage_from_transcript("garbage \x00 not json") == {}, \
        "an unparsable transcript yields nothing rather than raising"

    # A project holds one active job at a time, so the unmetered worker runs on a second
    # project with the same single-task plan.
    project2 = store.create_project(ProjectInput(name="Unmetered worker",
                                                 brief="Create a portfolio with visible contact information."))
    with store.tx() as db:
        db.execute("UPDATE projects SET plan=?,revision=1,state='awaiting_plan' WHERE id=?",
                   (flat_plan(1).model_dump_json(), project2["id"]))
    project2 = store.project(project2["id"])  # the state and revision moved under us
    job2 = build_job(store, project2)
    ParallelBuilder(settings, store, worker=SilentWorker()).build_tasks(
        project2, job2, flat_plan(1), lambda task, ctx: f"YOUR TASK ({task.id}) do it",
        lambda: False)
    silent = store.all("SELECT kind FROM events WHERE job_id=? AND kind='worker.usage'",
                       (job2["id"],))
    assert silent == [], "nothing is invented when the harness reports no usage"
