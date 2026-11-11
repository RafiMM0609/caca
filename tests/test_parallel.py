"""Parallel worker waves and the merge barrier.

A fake harness stands in for Pi so these tests are deterministic and offline. It writes
files and can be told to sleep, so overlap and the merge barrier are observable rather
than assumed.
"""
import threading
import time

import pytest

from factory import products
from factory.db import Store
from factory.models import Plan, ProjectInput
from factory.parallel import ParallelBuilder


def task(name, deps=None):
    return {"id": name, "title": f"Task {name}", "instructions": f"Implement the {name} part.",
            "acceptance": ["works"], "depends_on": deps or []}


class FakeWorker:
    """Stands in for PiWorker. Records concurrency and writes a file per task."""

    def __init__(self, sleep=0.0, conflict_on=None, shared_file=False):
        self.sleep = sleep
        self.conflict_on = conflict_on
        self.shared_file = shared_file
        self.active = 0
        self.peak = 0
        self.lock = threading.Lock()
        self.order = []

    def run_task(self, worktree, instruction, model="", timeout=0, cancelled=lambda: False,
                 skills=()):
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            same = self.shared_file or (self.conflict_on and self.conflict_on in instruction)
            name = "shared.txt" if same else self._name(instruction)
            # Distinct content per worker is what actually produces a merge conflict.
            (worktree / name).write_text(f"{self._name(instruction)} wrote this\n")
            with self.lock:
                self.order.append(name)
            time.sleep(self.sleep)
            return "worker transcript"
        finally:
            with self.lock:
                self.active -= 1

    @staticmethod
    def _name(instruction):
        marker = "YOUR TASK ("
        start = instruction.index(marker) + len(marker)
        task_id = instruction[start:instruction.index(")", start)]
        return f"{task_id}.txt"


@pytest.fixture
def env(tmp_path):
    from factory.config import Settings
    settings = Settings(data=tmp_path, operator_token="o" * 40, release_token="r" * 40,
                        model="test/model", pi="/usr/bin/true", worker_concurrency=4)
    store = Store(tmp_path / "factory.sqlite3")
    project = store.create_project(ProjectInput(name="Parallel demo", brief="Build a product with several parts."))
    return settings, store, project


def run(env, plan, worker, review=None):
    settings, store, project = env
    builder = ParallelBuilder(settings, store, worker=worker)
    job = {"id": "job-1", "payload": "{}"}
    builder.build_tasks(project, job, plan, lambda task, ctx: f"YOUR TASK ({task.id}) do it",
                        lambda: False, review)
    root = products.product_root(settings.data, project["id"])
    return root, builder


def test_independent_tasks_run_together(env):
    plan = Plan(summary="Two independent parts of the product.", tasks=[task("left"), task("right"),
                                                                       task("join", ["left", "right"])])
    worker = FakeWorker(sleep=0.3)
    root, builder = run(env, plan, worker)
    # left and right overlap; join waits for both, so peak concurrency is 2.
    assert worker.peak == 2
    assert sorted(p.name for p in root.iterdir() if p.is_file()) == ["join.txt", "left.txt", "right.txt"]


def test_dependent_task_waits_for_its_dependency(env):
    plan = Plan(summary="A chain where each step needs the previous.", tasks=[task("first"), task("second", ["first"])])
    worker = FakeWorker()
    root, builder = run(env, plan, worker)
    assert worker.peak == 1, "a dependent task must not run alongside its dependency"
    assert (root / "second.txt").exists()


def test_workers_are_recorded_with_their_outcome(env):
    plan = Plan(summary="One task that should be tracked as a worker.", tasks=[task("solo")])
    settings, store, project = env
    root, builder = run(env, plan, FakeWorker())
    records = store.workers_for("job-1")
    assert len(records) == 1
    assert records[0]["task_id"] == "solo"
    assert records[0]["state"] == "merged"
    assert records[0]["branch"] == "worker/solo"


def test_merge_barrier_runs_after_the_whole_wave(env):
    """No task is merged while a sibling is still running."""
    plan = Plan(summary="Two parallel parts merged only after both finish.",
                tasks=[task("a"), task("b")])
    settings, store, project = env
    merged_at = []

    original = products.merge_branch

    def spy(root, branch, message):
        merged_at.append((branch, len(store.workers_for("job-1")),
                          len([r for r in store.workers_for("job-1") if r["state"] == "merged"])))
        return original(root, branch, message)

    products.merge_branch = spy
    try:
        worker = FakeWorker(sleep=0.2)
        run(env, plan, worker)
    finally:
        products.merge_branch = original

    # Both workers must already exist before the first merge happens.
    assert merged_at, "merges should have happened"
    assert all(count == 2 for _, count, _ in merged_at)
    # And the first merge happens before anything is marked merged.
    assert merged_at[0][2] == 0


def test_conflict_is_detected_and_the_wave_recovers_on_retry(env):
    """Two workers touching one file: the barrier detects the conflict and retries.

    The retry re-runs only the unmerged task against the updated target, so the second
    attempt integrates cleanly. This is the intended recovery, not a silent overwrite.
    """
    plan = Plan(summary="Two parts that both edit the same file.", tasks=[task("one"), task("two")])
    settings, store, project = env

    class NoFixer(FakeWorker):
        def run_task(self, worktree, instruction, **kwargs):
            if "Integration conflict" in instruction:
                raise RuntimeError("fixer unavailable")
            return super().run_task(worktree, instruction, **kwargs)

    builder = ParallelBuilder(settings, store, worker=NoFixer(shared_file=True))
    builder.build_tasks(project, {"id": "job-1", "payload": "{}"}, plan,
                        lambda task, ctx: f"YOUR TASK ({task.id}) do it", lambda: False)

    kinds = [e["kind"] for e in store.all("SELECT kind FROM events WHERE job_id='job-1'")]
    assert "merge.conflict" in kinds, "the barrier must record the conflict"
    assert "fixer.spawning" in kinds, "a fixer must be attempted"
    assert "fixer.failed" in kinds, "an unavailable fixer must be visible"
    assert "wave.retry" in kinds, "the wave must be retried rather than abandoned"
    states = {w["task_id"]: w["state"] for w in store.workers_for("job-1")}
    assert states == {"one": "merged", "two": "merged"}


def test_a_worker_that_always_fails_stops_after_bounded_attempts(env):
    """A worker that never produces a mergeable branch must not loop forever."""
    plan = Plan(summary="A task whose worker never succeeds.", tasks=[task("doomed")])
    settings, store, project = env

    class Broken(FakeWorker):
        def run_task(self, worktree, instruction, **kwargs):
            raise RuntimeError("harness unavailable")

    builder = ParallelBuilder(settings, store, worker=Broken())
    with pytest.raises(ValueError, match="failed after bounded repair attempts"):
        builder.build_tasks(project, {"id": "job-1", "payload": "{}"}, plan,
                            lambda task, ctx: f"YOUR TASK ({task.id}) do it", lambda: False)
    root = products.product_root(settings.data, project["id"])
    assert not (root / "doomed.txt").exists()
    kinds = [e["kind"] for e in store.all("SELECT kind FROM events WHERE job_id='job-1'")]
    assert kinds.count("wave.retry") <= settings.max_repairs


def test_rejected_review_blocks_the_merge(env):
    """A rejected review must never reach the target branch."""
    plan = Plan(summary="One task whose review will reject it.", tasks=[task("bad")])
    settings, store, project = env
    builder = ParallelBuilder(settings, store, worker=FakeWorker())
    with pytest.raises(ValueError, match="failed after bounded repair attempts"):
        builder.build_tasks(project, {"id": "job-1", "payload": "{}"}, plan,
                            lambda task, ctx: f"YOUR TASK ({task.id}) do it", lambda: False,
                            lambda task, worktree, diff, base: {"approved": False, "findings": ["nope"]})
    root = products.product_root(settings.data, project["id"])
    assert not (root / "bad.txt").exists(), "rejected work must not be merged"
    assert all(w["state"] != "merged" for w in store.workers_for("job-1"))
    kinds = [e["kind"] for e in store.all("SELECT kind FROM events WHERE job_id='job-1'")]
    assert "worker.rejected" in kinds


def test_cancellation_stops_between_waves(env):
    from factory.process import Cancelled
    plan = Plan(summary="A plan that is cancelled before any work starts.", tasks=[task("x")])
    settings, store, project = env
    builder = ParallelBuilder(settings, store, worker=FakeWorker())
    with pytest.raises(Cancelled):
        builder.build_tasks(project, {"id": "job-1", "payload": "{}"}, plan,
                            lambda task, ctx: "do it", lambda: True)


class FeedbackWorker(FakeWorker):
    """Records the feedback each attempt receives, so the retry prompt is observable."""

    def __init__(self, fix_on_attempt=2):
        super().__init__()
        self.instructions = []
        self.fix_on_attempt = fix_on_attempt

    def run_task(self, worktree, instruction, model="", timeout=0, cancelled=lambda: False,
                 skills=()):
        self.instructions.append(instruction)
        return super().run_task(worktree, instruction, model, timeout, cancelled)


def test_a_rejected_task_is_told_what_the_reviewer_said(env):
    """A retry must receive the findings, not repeat a prompt identical to the failed one.

    This is the defect that let a task rejected twice for an unrequested dependency merge on
    its third attempt with the dependency still present: the worker was never told.
    """
    plan = Plan(summary="One task whose first attempt is rejected.", tasks=[task("solo")])
    settings, store, project = env
    worker = FeedbackWorker()

    def review(task, worktree, diff, base):
        # Reject the first attempt only.
        if len(worker.instructions) < 2:
            return {"approved": False,
                    "findings": ["package.json adds better-sqlite3, an unrequested dependency"]}
        return {"approved": True, "findings": []}

    builder = ParallelBuilder(settings, store, worker=worker)
    builder.build_tasks(project, {"id": "job-1", "payload": "{}"}, plan,
                        lambda t, ctx: (f"YOUR TASK ({t.id}) do it"
                                        + (f"\n\nPREVIOUS ATTEMPT FEEDBACK:\n{ctx['feedback']}"
                                           if ctx.get("feedback") else "")),
                        lambda: False, review)

    assert len(worker.instructions) == 2
    first, second = worker.instructions
    assert "PREVIOUS ATTEMPT FEEDBACK" not in first, "the first attempt has nothing to be told"
    assert "PREVIOUS ATTEMPT FEEDBACK" in second, "the retry must carry the reviewer's findings"
    assert "better-sqlite3" in second


def test_approval_after_a_rejection_is_recorded_as_a_disagreement(env):
    """An approval that reverses an earlier rejection must be visible, not silently green."""
    plan = Plan(summary="One task approved only on its second attempt.", tasks=[task("solo")])
    settings, store, project = env
    worker = FeedbackWorker()

    def review(task, worktree, diff, base):
        if len(worker.instructions) < 2:
            return {"approved": False, "findings": ["the API route is missing"]}
        return {"approved": True, "findings": []}

    builder = ParallelBuilder(settings, store, worker=worker)
    builder.build_tasks(project, {"id": "job-1", "payload": "{}"}, plan,
                        lambda t, ctx: f"YOUR TASK ({t.id}) do it", lambda: False, review)

    events = [e["kind"] for e in store.all("SELECT kind FROM events WHERE job_id='job-1'")]
    assert "review.flaky_approval" in events
    record = store.workers_for("job-1")[0]
    assert record["state"] == "merged", "the verdict stands; this is a warning, not a block"
    assert "flaky_approval" in (record["detail"] or "")


def test_a_merged_worktree_is_removed_from_disk(settings, store, project, plan):
    """A merged branch leaves a dead worktree holding a full `node_modules`.

    Removal only happened when a retry reused the branch, so a successful build leaked every
    worktree it created: two builds left 2.4GB behind before anyone looked.
    """
    from pathlib import Path

    class Worker:
        def run_task(self, worktree, instruction, model="", timeout=0, cancelled=lambda: False,
                     skills=()):
            (Path(worktree) / "index.html").write_text("<title>x</title>")
            return "ok"

    builder = ParallelBuilder(settings, store, worker=Worker())
    job = {"id": "cleanup-job", "payload": "{}"}
    builder.build_tasks(store.project(project["id"]), job, plan,
                        lambda task, ctx: f"YOUR TASK ({task.id}) do it", lambda: False)

    root = products.product_root(settings.data, project["id"])
    # `list_worktrees` includes the primary checkout; only worker branches must be gone.
    leftover = [w for w in products.list_worktrees(root) if w.get("branch") != "main"]
    assert leftover == [], f"merged worktrees must not survive: {leftover}"

    workers_dir = settings.data / "workers" / "cleanup-job"
    assert not workers_dir.exists() or not any(workers_dir.iterdir()), \
        f"worktree directories left behind: {list(workers_dir.iterdir()) if workers_dir.exists() else []}"

    rows = store.workers_for("cleanup-job")
    assert rows and all(r["state"] == "merged" for r in rows), \
        "the merge must still be recorded even though the directory is gone"


def test_every_harness_run_receives_the_worker_budget(env):
    """A fixer edits code and rebuilds, so it needs the worker budget too.

    The same defect as the repair worker: `_spawn_fixer` called `run_task` with no `timeout`,
    inheriting the 600s one-shot-call budget instead of `worker_timeout`.
    """
    plan = Plan(summary="Two parts that both edit the same file.", tasks=[task("one"), task("two")])
    settings, store, project = env
    seen = []

    class Recorder(FakeWorker):
        def run_task(self, worktree, instruction, model="", timeout=0, cancelled=lambda: False,
                     skills=()):
            seen.append(("fixer" if "Integration conflict" in instruction else "task", timeout))
            return super().run_task(worktree, instruction, model=model, timeout=timeout,
                                    cancelled=cancelled, skills=skills)

    builder = ParallelBuilder(settings, store, worker=Recorder(shared_file=True))
    builder.build_tasks(project, {"id": "job-1", "payload": "{}"}, plan,
                        lambda task, ctx: f"YOUR TASK ({task.id}) do it", lambda: False)

    fixers = [timeout for role, timeout in seen if role == "fixer"]
    assert fixers, "this plan must have exercised the fixer, otherwise the test proves nothing"
    assert all(timeout == settings.worker_timeout for timeout in fixers), (
        f"fixer runs must use worker_timeout={settings.worker_timeout}, got {fixers}")
