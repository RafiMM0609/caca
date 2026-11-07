"""Per-product git repositories and worker worktrees.

Each product owns an independent git repository under ``data/products/<product>``.
Parallel workers never touch it directly: every worker gets its own worktree on a
dedicated branch, commits there, and the controller merges finished branches back
into the product's target branch one at a time.

This module is deliberately the only place that shells out to git. All commands are
argv arrays with no shell interpolation, matching the rest of the factory.
"""
import subprocess
from pathlib import Path

# Identity is fixed so generated commits are attributable in a repository the operator
# may later publish. It is not a credential and grants no access.
GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "Anton Software Factory",
    "GIT_AUTHOR_EMAIL": "factory@localhost",
    "GIT_COMMITTER_NAME": "Anton Software Factory",
    "GIT_COMMITTER_EMAIL": "factory@localhost",
}

# Never let a product repository inherit the operator's global git behavior.
GIT_CONFIG = (
    "-c", "commit.gpgsign=false",
    "-c", "core.hooksPath=/dev/null",
    "-c", "advice.detachedHead=false",
)


class GitError(RuntimeError):
    """A git command failed. The captured output is bounded and has no secrets."""


def _run(argv, cwd, timeout=120, env=None):
    try:
        result = subprocess.run(
            ["git", *GIT_CONFIG, *argv],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env if env is not None else _clean_env(),
        )
    except subprocess.TimeoutExpired as exc:
        raise GitError(f"git {argv[0]} timed out after {timeout}s") from exc
    except OSError as exc:
        raise GitError(f"git {argv[0]} could not start: {exc.__class__.__name__}") from exc
    if result.returncode:
        raise GitError(f"git {argv[0]} failed ({result.returncode}): {(result.stderr or result.stdout)[-2000:]}")
    return result.stdout.strip()


def _clean_env():
    import os
    # Product repositories must not read the operator's git configuration or credentials.
    env = {key: os.environ[key] for key in ("PATH", "HOME", "LANG") if key in os.environ}
    env.update(GIT_IDENTITY)
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def product_root(data: Path, product: str) -> Path:
    return Path(data) / "products" / product


def ensure_repo(data: Path, product: str, seed_files: dict[str, str] | None = None) -> Path:
    """Create the product repository on first use and return its path.

    A brand-new repository gets one initial commit so worktrees have a base. When
    ``seed_files`` is empty the repository starts with an empty tree, which keeps
    "build the application from scratch" as a legitimate starting point rather than
    silently forcing a template.
    """
    root = product_root(data, product)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not (root / ".git").exists():
        _run(["init", "-q", "-b", "main"], cwd=root)
        # A repository with no commits cannot host a worktree; make the base explicit.
        _run(["commit", "-q", "--allow-empty", "-m", "Initialize product repository"], cwd=root)
        if seed_files:
            _write(root, seed_files)
            _run(["add", "-A"], cwd=root)
            _run(["commit", "-q", "-m", "Add initial project files"], cwd=root)
    return root


def _write(root: Path, files: dict[str, str]):
    for name, content in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)


def target_branch(root: Path) -> str:
    return _run(["rev-parse", "--abbrev-ref", "HEAD"], cwd=root)


def head_commit(root: Path) -> str:
    return _run(["rev-parse", "HEAD"], cwd=root)


def worktree_add(root: Path, destination: Path, branch: str) -> Path:
    """Create an isolated worktree on a new branch based on the current target HEAD."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    _run(["worktree", "add", "-q", "-b", branch, str(destination)], cwd=root)
    return destination


def worktree_remove(root: Path, destination: Path):
    # Best effort: a stale worktree must not fail the job that is cleaning up.
    try:
        _run(["worktree", "remove", "--force", str(destination)], cwd=root)
    except GitError:
        pass
    try:
        _run(["worktree", "prune"], cwd=root)
    except GitError:
        pass


def branch_delete(root: Path, branch: str):
    try:
        _run(["branch", "-D", branch], cwd=root)
    except GitError:
        pass


def commit_all(worktree: Path, message: str) -> str:
    """Stage everything and commit. Returns the new commit id.

    An unchanged worktree is not an error: the worker may have decided no code change
    was required, and that is reported through the returned commit being unchanged.
    """
    _run(["add", "-A"], cwd=worktree)
    status = _run(["status", "--porcelain"], cwd=worktree)
    if not status:
        return head_commit(worktree)
    _run(["commit", "-q", "-m", message], cwd=worktree)
    return head_commit(worktree)


def diff_against(worktree: Path, base: str, limit=200_000) -> str:
    """Bounded unified diff of the worktree against a base commit, for independent review."""
    text = _run(["diff", "--no-color", "--no-ext-diff", base, "--"], cwd=worktree, timeout=60)
    return text[:limit]


def changed_files(worktree: Path, base: str) -> list[str]:
    out = _run(["diff", "--name-only", base, "--"], cwd=worktree, timeout=60)
    return [line for line in out.splitlines() if line.strip()]


def merge_branch(root: Path, branch: str, message: str):
    """Merge a worker branch into the checked-out target, one branch at a time.

    Conflicts are raised as GitError after the merge is aborted, so the target branch
    is never left half-merged. The caller decides whether to spawn a fixer worker.
    """
    try:
        _run(["merge", "--no-ff", "--no-edit", "-m", message, branch], cwd=root, timeout=120)
    except GitError:
        try:
            _run(["merge", "--abort"], cwd=root)
        except GitError:
            pass
        raise


def has_conflicts(root: Path) -> bool:
    return bool(_run(["diff", "--name-only", "--diff-filter=U"], cwd=root))


def checkout_target(root: Path):
    branch = target_branch(root)
    if branch != "main":
        _run(["checkout", "-q", "main"], cwd=root)


def current_branch(root: Path) -> str:
    return _run(["rev-parse", "--abbrev-ref", "HEAD"], cwd=root)


def list_worktrees(root: Path) -> list[dict]:
    out = _run(["worktree", "list", "--porcelain"], cwd=root)
    entries, current = [], {}
    for line in out.splitlines():
        if not line.strip():
            if current:
                entries.append(current)
            current = {}
        elif line.startswith("worktree "):
            current["path"] = line[len("worktree "):]
        elif line.startswith("branch "):
            current["branch"] = line[len("branch "):].removeprefix("refs/heads/")
        elif line.startswith("HEAD "):
            current["head"] = line[len("HEAD "):]
    if current:
        entries.append(current)
    return entries
