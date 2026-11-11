"""A task can opt into vendored design skills, and only from the vendor directory.

A skill is a file the model reads, so it only reaches a tool-using worker — established by
running Pi, not by reading its docs. `--no-skills` disables discovery, which is what stops a
worker reading configuration out of the product repository, while an explicitly named path
still loads.
"""
from pathlib import Path

import pytest

from factory.agents import PiWorker, PiWorkerError
from factory.models import Plan, ProjectInput, Task
from factory.parallel import ParallelBuilder

ROOT = Path(__file__).resolve().parent.parent


def make_skill(root, name):
    directory = root / name
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: A vendored design skill for tests.\n---\n"
        "Prefer a soft shadow to a hard border.\n")
    return directory


def test_a_task_may_declare_skills():
    task = Task.model_validate({"id": "ui", "title": "Build the interface",
                                "instructions": "Build the page.", "acceptance": ["works"],
                                "skills": ["emil-design-eng", "animate"]})
    assert task.skills == ["emil-design-eng", "animate"]


def test_a_task_may_declare_no_skills():
    task = Task.model_validate({"id": "api", "title": "Build the API",
                                "instructions": "Build the endpoint.", "acceptance": ["works"]})
    assert task.skills == []


@pytest.mark.parametrize("skills,reason", [
    (["Bad Name"], "uppercase and space"),
    (["animate", "animate"], "duplicate"),
    ([f"s{i}" for i in range(5)], "too many"),
])
def test_an_invalid_skill_list_is_rejected(skills, reason):
    with pytest.raises(ValueError):
        Task.model_validate({"id": "ui", "title": "Build the interface",
                             "instructions": "Build the page.", "acceptance": ["works"],
                             "skills": skills})


def test_names_resolve_to_directories_inside_the_vendor_root(tmp_path):
    make_skill(tmp_path, "animate")
    paths = PiWorker.skill_paths(["animate"], tmp_path)
    assert len(paths) == 1
    assert Path(paths[0]).is_dir()
    assert Path(paths[0]).name == "animate"


def test_no_names_resolves_to_no_flags(tmp_path):
    assert PiWorker.skill_paths([], tmp_path) == []


def test_a_name_that_escapes_the_vendor_root_is_refused(tmp_path):
    vendor = tmp_path / "skills"
    vendor.mkdir()
    make_skill(tmp_path / "elsewhere", "secret")
    with pytest.raises(PiWorkerError, match="escapes"):
        PiWorker.skill_paths(["../elsewhere/secret"], vendor)


def test_an_unknown_skill_is_an_error_not_a_silent_no_op(tmp_path):
    """A task that declares a skill and silently does not get it produces generic output."""
    (tmp_path / "skills").mkdir()
    with pytest.raises(PiWorkerError, match="Unknown skill"):
        PiWorker.skill_paths(["does-not-exist"], tmp_path / "skills")


def test_a_directory_without_a_manifest_is_not_a_skill(tmp_path):
    (tmp_path / "empty").mkdir()
    with pytest.raises(PiWorkerError, match="Unknown skill"):
        PiWorker.skill_paths(["empty"], tmp_path)


class RecordingWorker:
    """Captures the skills handed to each task so the plumbing is observable."""

    def __init__(self):
        self.calls = []

    def run_task(self, worktree, instruction, model="", timeout=0, cancelled=lambda: False,
                 skills=()):
        self.calls.append(list(skills))
        marker = "YOUR TASK ("
        start = instruction.index(marker) + len(marker)
        task_id = instruction[start:instruction.index(")", start)]
        (Path(worktree) / f"{task_id}.txt").write_text(f"{task_id}\n")
        return "worker transcript"


@pytest.fixture
def env(tmp_path):
    from factory.config import Settings
    settings = Settings(data=tmp_path, operator_token="o" * 40, release_token="r" * 40,
                        model="test/model", pi="/usr/bin/true", worker_concurrency=2)
    from factory.db import Store
    store = Store(tmp_path / "factory.sqlite3")
    project = store.create_project(ProjectInput(name="Skill demo",
                                                brief="A product with a UI and an API part."))
    return settings, store, project


def test_only_the_task_that_declares_a_skill_receives_it(env):
    """Skills are per task, so an API worker is not charged for design guidance."""
    settings, store, project = env
    plan = Plan.model_validate({
        "summary": "One interface task with skills, one API task without.",
        "tasks": [
            {"id": "ui", "title": "Build the interface",
             "instructions": "Build the page.", "acceptance": ["works"],
             "skills": ["emil-design-eng"]},
            {"id": "api", "title": "Build the API",
             "instructions": "Build the endpoint.", "acceptance": ["works"]},
        ],
        "test_flows": []})
    worker = RecordingWorker()
    ParallelBuilder(settings, store, worker=worker).build_tasks(
        store.project(project["id"]), {"id": "job-1", "payload": "{}"}, plan,
        lambda task, ctx: f"YOUR TASK ({task.id}) do it", lambda: False)
    assert ["emil-design-eng"] in worker.calls
    assert [] in worker.calls, "the API task must not carry a design skill"


def test_the_argv_adds_an_explicit_skill_alongside_no_skills(monkeypatch, tmp_path):
    """The exact flags matter: discovery off, the named path still on."""
    from factory.config import Settings

    vendor = tmp_path / "skills"
    make_skill(vendor, "animate")
    settings = Settings(data=tmp_path, operator_token="o" * 40, release_token="r" * 40,
                        model="test/model", pi="/tmp/fake-pi")
    monkeypatch.setenv("FACTORY_SKILLS_DIR", str(vendor))

    captured = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        return '{"type":"message_end","message":{"role":"assistant","content":[{"type":"text","text":"ok"}]}}\n'

    monkeypatch.setattr("factory.agents.run", fake_run)
    PiWorker(settings).run_task(tmp_path, "do it", skills=["animate"])
    argv = captured["argv"]
    assert "--no-skills" in argv, "discovery must stay disabled"
    assert "--skill" in argv, "an explicit skill must be passed"
    assert argv[argv.index("--skill") + 1] == str((vendor / "animate").resolve())


def test_the_vendored_skills_resolve_from_the_repository(settings=None):
    """Every vendored skill must be findable by name, or the plan cannot reference them."""
    from factory.config import Settings

    config = Settings(data=ROOT / "data", operator_token="o" * 40, release_token="r" * 40,
                      model="test/model")
    root = config.skills_dir()
    assert root.is_dir(), f"skills directory missing: {root}"
    for name in ("emil-design-eng", "animate", "apple-design", "mobile-native", "break-ui",
                 "design-taste-frontend"):
        paths = PiWorker.skill_paths([name], root)
        assert Path(paths[0]).name == name


def test_every_vendored_skill_declares_a_name_matching_its_directory():
    """A mismatched frontmatter name makes a skill hard to reason about when upgrading."""
    root = Path(__file__).resolve().parent.parent / "skills"
    for directory in sorted(p for p in root.iterdir() if p.is_dir()):
        text = (directory / "SKILL.md").read_text()
        assert text.startswith("---"), f"{directory.name} has no frontmatter"
        assert f"name: {directory.name}" in text.split("---")[1], \
            f"{directory.name} declares a different name"


def test_the_vendor_record_names_a_source_and_a_licence():
    text = (Path(__file__).resolve().parent.parent / "skills" / "VENDORED.md").read_text()
    assert "github.com/emilkowalski/skills" in text
    assert "github.com/Leonxlnx/taste-skill" in text
    assert "MIT" in text
    assert "Commit" in text, "an unpinned vendored skill can change under the build"


def test_every_vendored_skill_directory_is_named_in_the_vendor_record():
    """A skill added without a pinned source defeats the reason the vendor record exists."""
    root = Path(__file__).resolve().parent.parent / "skills"
    record = (root / "VENDORED.md").read_text()
    for directory in sorted(p for p in root.iterdir() if p.is_dir()):
        assert f"`{directory.name}`" in record, f"{directory.name} has no entry in VENDORED.md"
