"""Wall-clock and worker-environment behaviour measured on the Toko Roti rebuild.

Three changes came out of that build, and each is asserted here because each one is invisible
in the output: a longer deadline, a retry that is told why it failed, and a browser the worker
can actually see. None of them changes what a good build produces, so a test that only checked
the artifact would pass whether the change was there or not.
"""
import os
from pathlib import Path

import pytest

from factory.agents import PiWorker, _playwright_browsers_path
from factory.config import Settings
from factory.models import Plan, ProjectInput
from factory.parallel import ParallelBuilder, WorkerOutcome, _failure_note

ROOT = Path(__file__).resolve().parent.parent


def settings_at(tmp_path, **overrides):
    base = dict(data=tmp_path, operator_token="o" * 40, release_token="r" * 40,
                model="test/model", pi="/usr/bin/true")
    base.update(overrides)
    return Settings(**base)


# -- 1. a skill task gets the longer clock --------------------------------------------------

def test_a_skill_task_receives_the_skill_timeout_and_a_plain_task_does_not(tmp_path):
    """The bound is what reaches `run_task`, not only what config says.

    A design task iterates: build, look at the page, adjust. The flat 1800s bound cut that off
    mid-iteration on the Toko Roti rebuild, and the same task then finished in 585s.
    """
    from factory.db import Store

    settings = settings_at(tmp_path, agent_timeout=600, worker_timeout=1800,
                           skill_worker_timeout=3600)
    settings.validate()
    store = Store(tmp_path / "factory.sqlite3")
    project = store.create_project(ProjectInput(name="Timeout demo",
                                                brief="A product with a UI part and an API part."))
    plan = Plan.model_validate({
        "summary": "One design task and one API task.",
        "tasks": [
            {"id": "ui", "title": "Build the interface",
             "instructions": "Build the page.", "acceptance": ["works"],
             "skills": ["emil-design-eng"]},
            {"id": "api", "title": "Build the API",
             "instructions": "Build the endpoint.", "acceptance": ["works"]},
        ],
        "test_flows": []})

    seen = {}

    class RecordingWorker:
        def run_task(self, worktree, instruction, model="", timeout=0, cancelled=lambda: False,
                     skills=()):
            marker = "YOUR TASK ("
            start = instruction.index(marker) + len(marker)
            task_id = instruction[start:instruction.index(")", start)]
            seen[task_id] = timeout
            (Path(worktree) / f"{task_id}.txt").write_text(f"{task_id}\n")
            return "worker transcript"

    ParallelBuilder(settings, store, worker=RecordingWorker()).build_tasks(
        store.project(project["id"]), {"id": "job-1", "payload": "{}"}, plan,
        lambda task, ctx: f"YOUR TASK ({task.id}) do it", lambda: False)

    assert seen["ui"] == 3600, "a task carrying a skill must get the design clock"
    assert seen["api"] == 1800, "an API-only task has no design loop to fund"


def test_a_skill_timeout_below_the_worker_timeout_is_rejected(tmp_path):
    """A shorter skill clock would silently shorten the tasks that need it most."""
    settings = settings_at(tmp_path, worker_timeout=1800, skill_worker_timeout=900)
    with pytest.raises(ValueError, match="Skill worker timeout"):
        settings.validate()


def test_a_skill_timeout_beyond_the_job_timeout_is_rejected(tmp_path):
    """A worker that outlives its job dies on a different clock and looks unexplained."""
    settings = settings_at(tmp_path, job_timeout=1800, skill_worker_timeout=3600)
    with pytest.raises(ValueError, match="Skill worker timeout must not exceed the job timeout"):
        settings.validate()


def test_the_skill_timeout_defaults_above_the_worker_timeout():
    settings = Settings(data=Path("/tmp/wc-default"), operator_token="o" * 40,
                        release_token="r" * 40)
    settings.validate()
    assert settings.skill_worker_timeout > settings.worker_timeout


# -- 2. a retry is told why the previous attempt failed ------------------------------------

def test_a_deadline_retry_is_told_what_happened_and_what_to_change():
    """An empty feedback list is why attempt 2 re-explored a design attempt 1 had cut short."""
    outcome = WorkerOutcome("build-homepage", "failed", "worker/build-homepage",
                            detail={"error": "PiWorkerError",
                                    "message": "Process exceeded its 1800s deadline and was stopped"})
    note = _failure_note(outcome)
    assert "PiWorkerError" in note
    assert "1800s deadline" in note
    assert "ran out of wall-clock time" in note
    assert "Do not repeat exploratory passes" in note


def test_a_non_deadline_failure_gets_guidance_that_is_not_about_time():
    outcome = WorkerOutcome("api", "failed", "worker/api",
                            detail={"error": "OSError", "message": "No space left on device"})
    note = _failure_note(outcome)
    assert "OSError" in note
    assert "No space left on device" in note
    assert "exploratory passes" not in note


def test_a_failure_without_a_message_still_produces_a_usable_line():
    outcome = WorkerOutcome("api", "failed", "worker/api", detail={})
    note = _failure_note(outcome)
    assert "Unknown failure" in note
    assert note.strip().endswith(".")


def test_review_findings_still_win_over_the_generic_failure_note():
    """A specific finding must never be replaced by 'the process timed out'."""
    import inspect

    source = inspect.getsource(ParallelBuilder.build_tasks)
    assert "if reported:" in source, "a review verdict must take precedence"
    assert "findings[outcome.task_id] = [_failure_note(outcome)]" in source


# -- 3. a worker can see the browser the host already has -----------------------------------

def test_the_browser_directory_is_exported_when_it_exists(tmp_path, monkeypatch):
    browsers = tmp_path / "ms-playwright"
    (browsers / "chromium-1248").mkdir(parents=True)
    monkeypatch.setenv("FACTORY_PLAYWRIGHT_BROWSERS", str(browsers))

    env = PiWorker(settings_at(tmp_path)).worker_env(tmp_path / "home")
    assert env["PLAYWRIGHT_BROWSERS_PATH"] == str(browsers)


def test_no_browser_variable_when_the_directory_is_absent(tmp_path, monkeypatch):
    """A variable pointing at a missing path sends the worker looking in the wrong place."""
    monkeypatch.setenv("FACTORY_PLAYWRIGHT_BROWSERS", str(tmp_path / "does-not-exist"))
    assert _playwright_browsers_path() == ""
    env = PiWorker(settings_at(tmp_path)).worker_env(tmp_path / "home")
    assert "PLAYWRIGHT_BROWSERS_PATH" not in env


def test_an_empty_browser_directory_is_not_exported(tmp_path, monkeypatch):
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("FACTORY_PLAYWRIGHT_BROWSERS", str(empty))
    assert _playwright_browsers_path() == ""


def test_the_browser_variable_does_not_widen_the_worker_home(tmp_path, monkeypatch):
    """Isolation is the point: ~/.ssh must stay unreachable while browsers are visible."""
    browsers = tmp_path / "ms-playwright"
    (browsers / "chromium-1248").mkdir(parents=True)
    monkeypatch.setenv("FACTORY_PLAYWRIGHT_BROWSERS", str(browsers))
    monkeypatch.setenv("FACTORY_OPERATOR_TOKEN", "o" * 40)

    home = tmp_path / "isolated-home"
    env = PiWorker(settings_at(tmp_path)).worker_env(home)
    assert env["HOME"] == str(home), "HOME must stay the per-worker directory"
    assert env["XDG_CACHE_HOME"] == str(home / ".cache")
    assert "FACTORY_OPERATOR_TOKEN" not in env
    # The only path the worker gains is the browser directory itself.
    assert env["PLAYWRIGHT_BROWSERS_PATH"] == str(browsers)


def test_the_real_host_browser_path_resolves_when_present(monkeypatch):
    """On a host with Playwright installed, the default location is what gets exported."""
    monkeypatch.delenv("FACTORY_PLAYWRIGHT_BROWSERS", raising=False)
    found = _playwright_browsers_path()
    home = os.environ.get("HOME", "")
    if home and (Path(home) / ".cache" / "ms-playwright").is_dir():
        assert found == str(Path(home) / ".cache" / "ms-playwright")
    else:
        assert found == ""


def test_no_worker_call_site_falls_back_to_the_agent_clock():
    """Every harness-worker invocation must pass `worker_timeout` explicitly.

    `run_task` defaults to `agent_timeout` (600s) when `timeout` is omitted. That default is
    correct for a one-shot structured call and wrong for a worker that edits a tree and builds
    it: measured live, a worker was killed at 615s (agent_timeout + 15) after writing 1,784
    lines of Zig that were then never committed, and the retry restarted from zero.

    The fix landed piecemeal — implementation workers, then repair workers, then the merge
    fixer — because each call site had to be found by hitting the bug. This test asserts the
    property rather than the three fixes: for every `run_task` call in `factory/`, a `timeout`
    keyword must be present. A new call site that forgets it fails here instead of in a live run
    ten minutes in.
    """
    import ast
    from pathlib import Path

    calls = []
    for path in sorted((Path(__file__).resolve().parent.parent / "factory").glob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "run_task":
                keywords = {keyword.arg for keyword in node.keywords}
                calls.append((path.name, node.lineno, keywords))

    assert calls, "no run_task call site found; the assertion below would be vacuous"
    missing = [(name, line) for name, line, keywords in calls if "timeout" not in keywords]
    assert not missing, (
        "these run_task calls inherit agent_timeout (600s) instead of worker_timeout: "
        + ", ".join(f"{name}:{line}" for name, line in missing))


# -- the budget is recorded, so a live timeout is answerable from evidence ------------------

def test_the_worker_budget_that_was_used_is_recorded_before_the_run(tmp_path):
    """A live run was killed at 615s while config said worker_timeout=1800.

    Nothing in the evidence named the clock the worker had actually been given, so the cause
    had to be reconstructed by auditing every call site. The event makes the next occurrence a
    one-line read: the budget passed in, the clock kind, and both configured values.
    """
    from factory.db import Store

    settings = settings_at(tmp_path, agent_timeout=600, worker_timeout=1800,
                           skill_worker_timeout=3600)
    settings.validate()
    store = Store(tmp_path / "factory.sqlite3")
    project = store.create_project(ProjectInput(name="Budget demo",
                                                brief="A product with one task to build."))
    plan = Plan.model_validate({
        "summary": "A plan with exactly one task to build.",
        "tasks": [{"id": "only", "title": "Build it",
                   "instructions": "Build the thing the brief asks for.", "acceptance": ["works"]}],
        "test_flows": []})

    class RecordingWorker:
        def run_task(self, worktree, instruction, model="", timeout=0, cancelled=lambda: False,
                     skills=()):
            (Path(worktree) / "x.txt").write_text("x\n")
            return "transcript"

    ParallelBuilder(settings, store, worker=RecordingWorker()).build_tasks(
        store.project(project["id"]), {"id": "job-budget", "payload": "{}"}, plan,
        lambda task, ctx: f"YOUR TASK ({task.id}) do it", lambda: False)

    import json
    rows = [json.loads(row["detail"]) for row in store.all(
        "SELECT detail FROM events WHERE kind='worker.budget'")]
    assert rows, "the worker budget must be recorded before the worker runs"
    recorded = rows[0]
    assert recorded["timeout_seconds"] == 1800, recorded
    assert recorded["configured_worker_timeout"] == 1800
    assert recorded["configured_agent_timeout"] == 600
    assert recorded["uses_skill_clock"] is False


# -- a failed attempt keeps the work it produced --------------------------------------------

def test_a_worker_that_dies_mid_edit_commits_what_it_wrote(tmp_path):
    """Three attempts of ~1,310 lines each were thrown away because only success committed.

    The commit does not accept the work -- the worker is still failed and the task still
    retries. It stops the retry from starting at zero and gives the operator something to
    inspect.
    """
    import json

    from factory.db import Store

    settings = settings_at(tmp_path, worker_timeout=1800)
    settings.validate()
    store = Store(tmp_path / "factory.sqlite3")
    project = store.create_project(ProjectInput(
        name="Salvage demo", brief="A product whose worker dies halfway through writing."))
    plan = Plan.model_validate({
        "summary": "A plan whose only task will fail in the middle of writing its files.",
        "tasks": [{"id": "only", "title": "Build it",
                   "instructions": "Build the thing the brief asks for.", "acceptance": ["works"]}],
        "test_flows": []})

    class DyingWorker:
        """Writes real files, then reports a deadline -- exactly what the live run did."""

        def run_task(self, worktree, instruction, model="", timeout=0, cancelled=lambda: False,
                     skills=()):
            target = Path(worktree)
            (target / "src").mkdir(exist_ok=True)
            (target / "src" / "main.zig").write_text("// 400 lines the model already wrote\n")
            (target / "src" / "store.zig").write_text("// another 500 lines\n")
            raise RuntimeError("Process exceeded its 1800s deadline and was stopped")

    # A wave in which every task failed raises, which is correct: the build did fail. The
    # subject of this test is the salvage record, so the raise is expected and caught.
    with pytest.raises(ValueError):
        ParallelBuilder(settings, store, worker=DyingWorker()).build_tasks(
            store.project(project["id"]), {"id": "job-salvage", "payload": "{}"}, plan,
            lambda task, ctx: f"YOUR TASK ({task.id}) do it", lambda: False)

    # The work must exist in git, not only on disk, because the next attempt deletes the branch.
    rows = [json.loads(row["detail"]) for row in store.all(
        "SELECT detail FROM events WHERE kind='worker.salvaged'")]
    assert rows, "a failed attempt that wrote files must record what it salvaged"
    salvaged = rows[0]["files"]
    assert any("main.zig" in name for name in salvaged), salvaged
    assert any("store.zig" in name for name in salvaged), salvaged


def test_a_worker_that_writes_nothing_records_no_salvage(tmp_path):
    """An empty failure must not claim a salvage; a false record is worse than none."""
    from factory.db import Store

    settings = settings_at(tmp_path, worker_timeout=1800)
    settings.validate()
    store = Store(tmp_path / "factory.sqlite3")
    project = store.create_project(ProjectInput(
        name="Empty failure", brief="A product whose worker fails without writing anything."))
    plan = Plan.model_validate({
        "summary": "A plan whose only task fails before it writes any files at all.",
        "tasks": [{"id": "only", "title": "Build it",
                   "instructions": "Build the thing the brief asks for.", "acceptance": ["works"]}],
        "test_flows": []})

    class FailingWorker:
        def run_task(self, worktree, instruction, model="", timeout=0, cancelled=lambda: False,
                     skills=()):
            raise RuntimeError("Process exceeded its 1800s deadline and was stopped")

    with pytest.raises(ValueError):
        ParallelBuilder(settings, store, worker=FailingWorker()).build_tasks(
            store.project(project["id"]), {"id": "job-empty", "payload": "{}"}, plan,
            lambda task, ctx: f"YOUR TASK ({task.id}) do it", lambda: False)

    assert not store.all("SELECT detail FROM events WHERE kind='worker.salvaged'")
