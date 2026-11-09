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
    """Best effort: a stale worktree must not fail the job that is cleaning up."""
    try:
        _run(["worktree", "remove", "--force", str(destination)], cwd=root)
    except GitError:
        pass
    try:
        _run(["worktree", "prune"], cwd=root)
    except GitError:
        pass


def branch_delete(root: Path, branch: str):
    """Best effort: deleting a branch that never existed is not an error."""
    try:
        _run(["branch", "-D", branch], cwd=root)
    except GitError:
        pass


def detached_worktree(root: Path, destination: Path, ref: str = "main") -> Path:
    """A worktree not attached to a branch, used by the conflict fixer.

    The fixer must produce the resolved target itself, so it cannot own a branch that
    would need a second merge.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    _run(["worktree", "add", "-q", "--detach", str(destination), ref], cwd=root)
    return destination


def fast_forward(root: Path, commit: str):
    """Advance the target to a commit that already contains its history."""
    _run(["merge", "--ff-only", commit], cwd=root)


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


# Lockfiles are generated, huge and mechanically reproducible, and a single real Nuxt
# lockfile is already larger than any sane review budget. Sending one to the reviewer
# crowds out the actual source diff and, worse, pushes real source files past the cut so
# the reviewer concludes they are missing and rejects correct work. They stay in
# `changed_files()` so the reviewer is still told they changed; only their contents are
# omitted. This is a review-presentation choice: the verifier still builds the real
# lockfile and `npm ci` still fails if it disagrees with package.json.
LOCKFILE_NAMES = ("package-lock.json", "npm-shrinkwrap.json", "pnpm-lock.yaml", "yarn.lock", "bun.lockb")
LOCKFILE_PATHSPECS = [f":(exclude,glob)**/{name}" for name in LOCKFILE_NAMES]


def diff_against(worktree: Path, base: str, limit=200_000) -> str:
    """Bounded unified diff of the worktree against a base, for independent review.

    Truncation is marked explicitly. A silently cut diff once made a reviewer reject a
    complete, correct change because the tail looked absent; the note tells it the rest
    exists and that "file not shown" is not evidence that a file is missing.
    """
    text = _run(["diff", "--no-color", "--no-ext-diff", base, "--", ".", *LOCKFILE_PATHSPECS],
                cwd=worktree, timeout=60)
    if len(text) <= limit:
        return text
    note = ("\n\n[DIFF TRUNCATED: showing the first %d of %d characters. Files listed as changed but "
            "not shown here were cut by this limit, not omitted by the worker. Do not report a file as "
            "missing on the strength of this truncation.]\n") % (limit, len(text))
    return text[:limit] + note


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


def export_tree(root: Path, destination: Path) -> Path:
    """Copy the tracked files of the target branch into a plain directory.

    The verifier expects a normal source tree, and it must never receive the git
    metadata or any leftover worktree state.
    """
    import shutil
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    listing = _run(["ls-tree", "-r", "--name-only", "HEAD"], cwd=root, timeout=60)
    for name in [line for line in listing.splitlines() if line.strip()]:
        source = root / name
        if not source.is_file() or source.is_symlink():
            continue
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    return destination


def tracked_files(worktree: Path) -> list[str]:
    """Files git knows about in a worktree, so a replacement can remove exactly those."""
    out = _run(["ls-files"], cwd=worktree, timeout=60)
    return [line for line in out.splitlines() if line.strip()]


def export_tree_worktree(worktree: Path, destination: Path) -> Path:
    """Copy a worktree's files, including uncommitted ones, without its git metadata."""
    import shutil
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    for path in sorted(worktree.rglob("*")):
        if ".git" in path.relative_to(worktree).parts:
            continue
        relative = path.relative_to(worktree)
        if path.is_symlink() or not path.is_file():
            continue
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
    return destination


def init_artifact_repo(root: Path, message: str) -> str:
    """Create a single-commit repository inside a published artifact and return the commit.

    Deliberately not a copy of the product repository's `.git`: a hand-off should not carry
    the operator's branches, stale worktree pointers, abandoned experiments or any remote
    configuration. `_run()` already pins a factory identity, disables hooks and gpg signing,
    and blanks the operator's git environment, so the commit is attributable and inert.
    """
    if not root.is_dir():
        raise GitError("Artifact directory does not exist")
    _run(["init", "-q", "-b", "main"], cwd=root)
    _run(["add", "-A"], cwd=root)
    _run(["commit", "-q", "-m", message], cwd=root)
    return head_commit(root)


def ancestors(root: Path, limit=500) -> set[str]:
    """Commits reachable from the target branch, for judging whether merged work survived.

    Resume needs a fact about the repository, not about the database: a worker row saying
    "merged" is only true while its commit is still on the branch. A reset, a rebase or a
    failed conflict resolution can leave the row behind.
    """
    out = _run(["rev-list", f"--max-count={limit}", "HEAD"], cwd=root, timeout=60)
    return {line.strip() for line in out.splitlines() if line.strip()}
