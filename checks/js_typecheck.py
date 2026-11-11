"""Operator-owned type gate for a JavaScript/TypeScript project tree.

A build can pass while shipping TypeScript that does not typecheck: `nuxt build` compiles
with type errors reported but non-fatal, and `vite build` does not typecheck `.vue` SFCs
at all. The reviewer reads the diff; this check runs the project's own declared
`typecheck` script, so the gate is whatever the project itself committed to.

Usage: python3 js_typecheck.py <artifact_dir> [--require-typecheck]

The check never executes project files directly: it runs `npm run typecheck` as an argv
array in the target directory, exactly as an operator would. Requires `npm` and `node` on
PATH; if either is absent the check fails rather than passing silently, because a gate
that cannot run must not report success.

A tree without a `typecheck` script is skipped by default (legacy projects still build),
but `--require-typecheck` turns that skip into a failure so an operator profile can
enforce the gate once a project declares it.
"""
import json
import subprocess
import sys
from pathlib import Path

TIMEOUT = 300
OUTPUT_TAIL = 4000


def main():
    if len(sys.argv) < 2:
        raise SystemExit("usage: js_typecheck.py <artifact_dir> [--require-typecheck]")
    require = "--require-typecheck" in sys.argv[1:]
    directories = [argument for argument in sys.argv[1:] if argument != "--require-typecheck"]
    if len(directories) != 1:
        raise SystemExit("usage: js_typecheck.py <artifact_dir> [--require-typecheck]")
    root = Path(directories[0]).resolve()
    if not root.is_dir():
        raise SystemExit(f"artifact directory not found: {root}")
    if not _tool_available("npm") or not _tool_available("node"):
        raise SystemExit("npm/node are not on PATH; the type gate cannot run, so it must not pass")

    manifest = root / "package.json"
    if not manifest.is_file():
        raise SystemExit(f"no package.json in {root}; nothing to typecheck")
    try:
        scripts = json.loads(manifest.read_text()).get("scripts") or {}
    except (OSError, ValueError) as exc:
        raise SystemExit(f"unreadable package.json in {root}: {exc}")
    if "typecheck" not in scripts:
        note = "no typecheck script in package.json; type gate skipped"
        if require:
            raise SystemExit(f"FAIL: {note} (--require-typecheck)")
        print(f"SKIP: {note}")
        return

    result = subprocess.run(["npm", "run", "typecheck"], cwd=root,
                            capture_output=True, text=True, timeout=TIMEOUT)
    if result.returncode:
        tail = (result.stderr or result.stdout or "").strip()[-OUTPUT_TAIL:]
        print("TYPECHECK FAILED:", tail if tail else f"npm run typecheck exited {result.returncode}")
        raise SystemExit(1)
    print(f"PASS: npm run typecheck in {root.name or root}")


def _tool_available(name: str) -> bool:
    try:
        result = subprocess.run([name, "--version"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


if __name__ == "__main__":
    main()
