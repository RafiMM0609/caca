"""Per-product repositories, worker worktrees and the plan DAG.

These tests run real git against temporary directories. They are not mocked, because
the merge and conflict behaviour is exactly what the factory relies on.
"""
import subprocess

import pytest

from factory import products
from factory.models import Plan


def task(name, depends_on=None):
    return {"id": name, "title": f"Task {name}", "instructions": f"Implement the {name} part of the product.",
            "acceptance": ["The part works"], "depends_on": depends_on or []}


def plan_of(*tasks):
    return Plan(summary="A product plan used to exercise dependency scheduling.", tasks=list(tasks))


# --- Plan DAG -------------------------------------------------------------------------

def test_independent_tasks_share_a_parallel_frontier():
    plan = plan_of(task("content"), task("layout"), task("homepage", ["content", "layout"]))
    assert [t.id for t in plan.ready_set([])] == ["content", "layout"]
    assert [t.id for t in plan.ready_set(["content"])] == ["layout"]
    assert [t.id for t in plan.ready_set(["content", "layout"])] == ["homepage"]


def test_forward_references_are_allowed_now_that_order_comes_from_the_graph():
    plan = plan_of(task("later", ["first"]), task("first"))
    assert plan.execution_order() == ["first", "later"]
    assert [t.id for t in plan.ready_set([])] == ["first"]


def test_cycle_is_rejected_instead_of_dropping_work():
    with pytest.raises(ValueError, match="cycle"):
        plan_of(task("a", ["b"]), task("b", ["a"]))


@pytest.mark.parametrize("deps,message", [(["a"], "itself"), (["missing"], "unknown")])
def test_invalid_dependencies_are_rejected(deps, message):
    with pytest.raises(ValueError, match=message):
        plan_of(task("a", deps))


def test_duplicate_task_ids_are_rejected():
    with pytest.raises(ValueError, match="unique"):
        plan_of(task("same"), task("same"))


def test_diamond_graph_runs_layers_in_order():
    plan = plan_of(task("base"), task("left", ["base"]), task("right", ["base"]),
                   task("join", ["left", "right"]))
    assert plan.execution_order() == ["base", "left", "right", "join"]
    assert [t.id for t in plan.ready_set(["base"])] == ["left", "right"]


# --- Product repositories -------------------------------------------------------------

@pytest.fixture
def data(tmp_path):
    return tmp_path / "data"


def test_product_repository_is_created_once(data):
    root = products.ensure_repo(data, "alpha")
    assert (root / ".git").is_dir()
    assert root == data / "products" / "alpha"
    # Idempotent: a second call must not reinitialize or drop history.
    head = products.head_commit(root)
    assert products.ensure_repo(data, "alpha") == root
    assert products.head_commit(root) == head


def test_products_are_isolated_from_each_other(data):
    alpha = products.ensure_repo(data, "alpha")
    beta = products.ensure_repo(data, "beta")
    (alpha / "a.txt").write_text("alpha\n")
    products.commit_all(alpha, "alpha work")
    assert not (beta / "a.txt").exists(), "one product must never see another product's files"
    assert products.changed_files(beta, products.head_commit(beta)) == []


def test_seeded_repository_starts_from_the_given_files(data):
    root = products.ensure_repo(data, "seeded", seed_files={"content/site.json": '{"brand":"A"}\n'})
    assert (root / "content" / "site.json").is_file()
    assert products.head_commit(root)


def test_worktrees_are_isolated_until_merged(data):
    root = products.ensure_repo(data, "demo")
    base = products.head_commit(root)
    one = products.worktree_add(root, data / "wt" / "one", "worker/one")
    two = products.worktree_add(root, data / "wt" / "two", "worker/two")

    (one / "one.txt").write_text("one\n")
    (two / "two.txt").write_text("two\n")
    products.commit_all(one, "worker one")
    products.commit_all(two, "worker two")

    # Neither change is visible in the target or in the sibling worktree yet.
    assert not (root / "one.txt").exists()
    assert not (root / "two.txt").exists()
    assert not (two / "one.txt").exists()
    assert products.changed_files(one, base) == ["one.txt"]
    assert products.changed_files(two, base) == ["two.txt"]


def test_sequential_merge_lands_every_worker_branch(data):
    root = products.ensure_repo(data, "demo")
    branches = {}
    for name in ("a", "b", "c"):
        work = products.worktree_add(root, data / "wt" / name, f"worker/{name}")
        (work / f"{name}.txt").write_text(f"{name}\n")
        products.commit_all(work, f"worker {name}")
        branches[name] = work
    for name in ("a", "b", "c"):
        products.merge_branch(root, f"worker/{name}", f"Merge worker {name}")
    assert sorted(p.name for p in root.iterdir() if p.is_file()) == ["a.txt", "b.txt", "c.txt"]


def test_conflicting_merge_is_reported_and_leaves_target_intact(data):
    root = products.ensure_repo(data, "demo")
    first = products.worktree_add(root, data / "wt" / "first", "worker/first")
    second = products.worktree_add(root, data / "wt" / "second", "worker/second")
    (first / "shared.txt").write_text("first version\n")
    products.commit_all(first, "first")
    (second / "shared.txt").write_text("second version\n")
    products.commit_all(second, "second")

    products.merge_branch(root, "worker/first", "Merge first")
    with pytest.raises(products.GitError):
        products.merge_branch(root, "worker/second", "Merge second")

    # The target must not be left half-merged, and the winning content must survive.
    assert not products.has_conflicts(root)
    assert (root / "shared.txt").read_text() == "first version\n"


def test_worktree_cleanup_removes_the_directory(data):
    root = products.ensure_repo(data, "demo")
    work = products.worktree_add(root, data / "wt" / "gone", "worker/gone")
    assert work.is_dir()
    products.worktree_remove(root, work)
    assert not work.exists()
    assert all(w["path"] == str(root) for w in products.list_worktrees(root))


def test_git_commands_never_prompt_or_use_operator_config(data):
    """A product repository must not read the operator's git config or credentials."""
    root = products.ensure_repo(data, "demo")
    env = products._clean_env()
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    # Commits are attributable to the factory, not to the operator's identity.
    author = subprocess.run(["git", "log", "-1", "--format=%an <%ae>"], cwd=root,
                            capture_output=True, text=True).stdout.strip()
    assert author == "Anton Software Factory <factory@localhost>"


def test_unborn_repository_can_host_worktrees(data):
    """Without an initial commit git refuses to create a worktree, so the base is explicit."""
    root = products.ensure_repo(data, "empty")
    assert products.head_commit(root)
    work = products.worktree_add(root, data / "wt" / "w", "worker/w")
    assert work.is_dir()
