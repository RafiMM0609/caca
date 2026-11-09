"""The plan must not spend a worker on work the factory already does.

The verifier runs the profile's install and build commands, the operator checks and every
declared test flow, after the merge barrier. A planner that adds "run npm ci, build, start the
server and smoke every endpoint" as a *task* spends a worker and a full model call to
reproduce that, and its result is thrown away.
"""
from pathlib import Path

import pytest

from factory.config import Settings
from factory.engine import PLANNER_PROMPT


def test_the_planner_is_told_that_verification_belongs_to_the_factory():
    text = PLANNER_PROMPT.lower()
    assert "factory verifies the merged result" in text
    assert "do not add a task that only re-runs installation" in text
    assert "duplicates the verifier" in text


def test_the_prompt_still_demands_test_flows():
    """Forbidding a verification *task* must not remove the tester contract."""
    assert "test_flows" in PLANNER_PROMPT
    assert "selectors" in PLANNER_PROMPT


def test_worker_timeout_is_separate_from_agent_timeout():
    """One model call and one worker run are different units of work."""
    settings = Settings(data=Path("/tmp/ct-check"), operator_token="o" * 40,
                        release_token="r" * 40)
    settings.validate()
    assert settings.worker_timeout > settings.agent_timeout, \
        "a worker may build and test its own work; a structured call may not"
    assert settings.worker_timeout <= settings.job_timeout, \
        "a single worker must not be allowed to outlive the whole job"


@pytest.mark.parametrize("value", [10, 20_000])
def test_an_unreasonable_worker_timeout_is_rejected(value):
    settings = Settings(data=Path("/tmp/ct-check"), operator_token="o" * 40,
                        release_token="r" * 40, worker_timeout=value)
    with pytest.raises(ValueError, match="Worker timeout"):
        settings.validate()


def test_a_worker_receives_the_worker_timeout_not_the_agent_timeout(tmp_path):
    """The bound is what actually reaches `run_task`, not only what config says."""
    from factory.db import Store
    from factory.models import Plan, ProjectInput
    from factory.parallel import ParallelBuilder

    settings = Settings(data=tmp_path, operator_token="o" * 40, release_token="r" * 40,
                        model="test/model", pi="/usr/bin/true", agent_timeout=600,
                        worker_timeout=1234)
    store = Store(tmp_path / "factory.sqlite3")
    project = store.create_project(ProjectInput(name="Timeout demo",
                                                brief="A product with a single part to build."))
    seen = {}

    class Worker:
        def run_task(self, worktree, instruction, model="", timeout=0, cancelled=lambda: False,
                 skills=()):
            seen["timeout"] = timeout
            (Path(worktree) / "index.html").write_text("<title>x</title>")
            return "ok"

    plan = Plan.model_validate({
        "summary": "A plan with one task, used to check the worker timeout.",
        "tasks": [{"id": "solo", "title": "Build the page",
                   "instructions": "Create the single page.", "acceptance": ["works"]}],
        "test_flows": []})
    ParallelBuilder(settings, store, worker=Worker()).build_tasks(
        store.project(project["id"]), {"id": "job-1", "payload": "{}"}, plan,
        lambda task, ctx: f"YOUR TASK ({task.id}) do it", lambda: False)
    assert seen["timeout"] == 1234, "the worker must not inherit the agent timeout"
