"""The job ceiling scales with the plan, and the run records where its time went.

A flat `job_timeout` is wrong in both directions: too small and a legitimate larger plan dies
with no defect to find, too large and a wedged job wastes hours. Waves drive the cost, because
tasks inside a wave run concurrently.
"""
import json
from pathlib import Path

import pytest

from factory.config import Settings
from factory.models import Plan, Profile, Review


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
        def run_task(self, worktree, instruction, model="", timeout=0, cancelled=lambda: False):
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
        def run_task(self, worktree, instruction, model="", timeout=0, cancelled=lambda: False):
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
