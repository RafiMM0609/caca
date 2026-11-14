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


def test_review_diff_excludes_lockfiles_but_still_lists_them(data):
    """A real lockfile is larger than the review budget and used to hide the source diff.

    A 290 KB Nuxt lockfile ate the whole 200 KB budget, so the reviewer saw only a
    truncated lockfile, concluded package.json and the page were missing, and rejected
    correct work. Lockfiles must stay listed as changed while their contents stay out.
    """
    root = products.ensure_repo(data, "demo")
    base = products.head_commit(root)
    (root / "app.vue").write_text("<template>hi</template>\n")
    (root / "package-lock.json").write_text('{"lockfileVersion": 3}\n')
    products.commit_all(root, "worker change")

    assert set(products.changed_files(root, base)) == {"app.vue", "package-lock.json"}
    diff = products.diff_against(root, base)
    assert "app.vue" in diff
    assert "package-lock.json" not in diff


def test_review_diff_marks_truncation_instead_of_cutting_silently(data):
    root = products.ensure_repo(data, "demo")
    base = products.head_commit(root)
    (root / "big.ts").write_text("\n".join(f"const value{index} = {index};" for index in range(20_000)))
    products.commit_all(root, "big change")

    diff = products.diff_against(root, base, limit=1_000)
    assert "DIFF TRUNCATED" in diff
    assert "Do not report a file as missing" in diff


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
    # The base commit now also carries `.gitignore`, which the factory writes so a worker's
    # build output cannot become the reviewer's diff. The assertion is about the merged worker
    # files, so it names them explicitly rather than demanding an otherwise empty tree.
    merged = {p.name for p in root.iterdir() if p.is_file()}
    assert {"a.txt", "b.txt", "c.txt"} <= merged
    assert all(name in merged for name in ("a.txt", "b.txt", "c.txt"))
    assert merged - {"a.txt", "b.txt", "c.txt"} <= {".gitignore"}


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


def test_changed_files_includes_a_brand_new_file(tmp_path):
    """`git diff --name-only <base>` never lists an untracked file.

    Measured: a worktree holding a new `main.zig` answered `changed_files() == []` while
    `git status` reported `?? main.zig`. Every caller runs after a commit, which is why this
    stayed hidden; the salvage path asks before committing and concluded the worker had written
    nothing, so three attempts of work were discarded on a live run.
    """
    from factory import products

    root = products.ensure_repo(tmp_path, "changed-files-demo")
    products.checkout_target(root)
    base = products.head_commit(root)
    worktree = tmp_path / "wt"
    products.worktree_add(root, worktree, "worker/new-file")

    (worktree / "created-by-worker.zig").write_text("// new file\n")

    assert "created-by-worker.zig" in products.changed_files(worktree, base)
    # And the tracked case still works, so the union did not break the original behaviour.
    products.commit_all(worktree, "wip")
    assert "created-by-worker.zig" in products.changed_files(worktree, base)


def test_changed_files_reports_nothing_for_an_untouched_worktree(tmp_path):
    """The union must not invent entries: a clean worktree still reports no changes."""
    from factory import products

    root = products.ensure_repo(tmp_path, "changed-files-clean")
    products.checkout_target(root)
    base = products.head_commit(root)
    worktree = tmp_path / "wt"
    products.worktree_add(root, worktree, "worker/clean")

    assert products.changed_files(worktree, base) == []


def test_a_product_repository_ignores_build_output_from_its_first_commit(tmp_path):
    """A worker's own build output must never become the reviewer's diff.

    Measured on a live Zig build: the worker ran `zig build` inside its worktree, `commit_all`
    ran `git add -A`, and the reviewer received "hundreds of `.zig-cache` files" as the change.
    It rejected the task for noise, the retry repeated the same build, and the job failed after
    three attempts -- for a reason that has nothing to do with the code.

    The list is kept in step with `artifact.EXCLUDED_DIRS`: a directory the factory will not
    publish is one a worker must not commit.
    """
    from factory import products
    from factory.artifact import EXCLUDED_DIRS

    root = products.ensure_repo(tmp_path, "ignore-demo")
    products.checkout_target(root)
    base = products.head_commit(root)
    worktree = tmp_path / "wt"
    products.worktree_add(root, worktree, "worker/builds")

    (worktree / ".zig-cache").mkdir()
    for index in range(50):
        (worktree / ".zig-cache" / f"obj{index}.o").write_text("cache\n")
    (worktree / "zig-out" / "bin").mkdir(parents=True)
    (worktree / "zig-out" / "bin" / "app").write_text("binary\n")
    (worktree / "node_modules" / "pkg").mkdir(parents=True)
    (worktree / "node_modules" / "pkg" / "index.js").write_text("module\n")
    (worktree / "src").mkdir()
    (worktree / "src" / "main.zig").write_text("const std = @import(\"std\");\n")

    products.commit_all(worktree, "worker output")

    recorded = products.changed_files(worktree, base)
    assert recorded == ["src/main.zig"], f"build output leaked into the diff: {recorded}"

    # Every excluded artifact directory must be named in the ignore file, so the two lists
    # cannot drift: a new EXCLUDED_DIRS entry with no ignore rule reintroduces this bug.
    ignore_text = (root / ".gitignore").read_text()
    for directory in EXCLUDED_DIRS - {".git", ".data"}:
        assert directory in ignore_text, f"{directory} is excluded from artifacts but not ignored"


def test_an_existing_repository_still_gets_the_ignore_file(tmp_path):
    """Repositories created before this rule must be repaired, not just new ones."""
    from factory import products

    root = products.ensure_repo(tmp_path, "old-demo")
    # Simulate a repository made by an older release: remove the ignore file from the tree and
    # commit the removal, so the file is genuinely absent from the checked-out branch.
    (root / ".gitignore").unlink()
    products.commit_all(root, "drop ignore file")
    assert not (root / ".gitignore").exists()

    products.ensure_repo(tmp_path, "old-demo")
    assert (root / ".gitignore").exists(), "an existing repository must be given the ignore file"
