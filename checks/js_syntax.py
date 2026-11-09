"""Operator-owned syntax gate for a built JavaScript artifact tree.

A build can pass while shipping JavaScript that does not parse: a bundler may emit a
tree that was produced from a stale cache, a generated file may be truncated, or an
`.mjs` entry can be malformed while `nuxt build` reported success on the parts it did
typecheck. The reviewer reads the diff; this check reads the actual bytes that will be
served, which is why it belongs in the verifier rather than in the review prompt.

Usage: python3 js_syntax.py <artifact_dir> [more dirs...]

Runs `node --check` on each JavaScript file. `node --check` parses without executing, so
generated application code is never run by this check. Requires `node` on PATH; if node
is absent the check fails rather than passing silently, because a gate that cannot run
must not report success.
"""
import subprocess
import sys
from pathlib import Path

SUFFIXES = (".js", ".mjs", ".cjs")
# Bundled dependency output is not the operator's to fix and would dominate the run.
SKIP_DIRS = {"node_modules", ".git", ".output", ".nuxt", ".cache", "dist"}


def candidates(root: Path):
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        if path.suffix.lower() not in SUFFIXES:
            continue
        if any(part in SKIP_DIRS for part in path.relative_to(root).parts):
            continue
        yield path


def main():
    if len(sys.argv) < 2:
        raise SystemExit("usage: js_syntax.py <artifact_dir> [more dirs...]")
    if not _node_available():
        raise SystemExit("node is not on PATH; the syntax gate cannot run, so it must not pass")

    failures = []
    checked = 0
    # A profile whose artifact may legitimately contain no JavaScript passes
    # `--allow-empty`. Without it, an empty scan is a failure: for a Nuxt build, "no JS in
    # the artifact" means the path or the layout is wrong, and reporting PASS would be a
    # false clearance. The flag makes that judgement explicit per profile instead of
    # guessing which case the operator is in.
    allow_empty = "--allow-empty" in sys.argv[1:]
    directories = [argument for argument in sys.argv[1:] if argument != "--allow-empty"]
    if not directories:
        raise SystemExit("usage: js_syntax.py [--allow-empty] <artifact_dir> [more dirs...]")

    for argument in directories:
        root = Path(argument).resolve()
        if not root.is_dir():
            raise SystemExit(f"artifact directory not found: {root}")
        for path in candidates(root):
            checked += 1
            result = subprocess.run(["node", "--check", str(path)],
                                    capture_output=True, text=True, timeout=120)
            if result.returncode:
                detail = (result.stderr or result.stdout).strip().splitlines()
                first = detail[0] if detail else "unknown parse error"
                failures.append(f"{path.relative_to(root)}: {first[:200]}")

    if failures:
        for finding in failures[:40]:
            print(f"SYNTAX ERROR: {finding}")
        if len(failures) > 40:
            print(f"... and {len(failures) - 40} more")
        raise SystemExit(1)
    if checked == 0:
        if allow_empty:
            print("PASS: no JavaScript files in the artifact (--allow-empty)")
            return
        # An empty scan usually means the glob or the artifact layout is wrong. Failing
        # loudly is the point: "no files matched" is not "no syntax errors".
        raise SystemExit("no JavaScript files found; check the artifact path and layout, "
                         "or pass --allow-empty if this artifact is not expected to ship JS")
    print(f"PASS: node --check on {checked} JavaScript files")


def _node_available() -> bool:
    try:
        result = subprocess.run(["node", "--version"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


if __name__ == "__main__":
    main()
