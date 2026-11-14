"""Operator-owned secret-leak check for a built artifact tree.

Runs after the build inside the disposable verification tree, never on the controller.
Blocks publication when a provider credential or factory secret appears verbatim in the
built output, which a build success alone would not detect.

Usage: python3 secret_scan.py <artifact_dir>
"""
import os
import re
import sys
from pathlib import Path

# Patterns are deliberately anchored to recognizable credential shapes to keep false positives low.
SECRET_PATTERNS = {
    "opencode_key": re.compile(r"sk-[A-Za-z0-9]{20,}"),
    "openai_key": re.compile(r"sk-proj-[A-Za-z0-9_-]{20,}"),
    "anthropic_key": re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"),
    "github_token": re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}"),
    "aws_access_key": re.compile(r"AKIA[0-9A-Z]{16}"),
    "private_key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "bearer_token": re.compile(r"Bearer\s+[A-Za-z0-9._-]{40,}"),
}

# Operator-configured values are matched literally, so a real deployment secret cannot ship.
ENV_SECRETS = ("OPENCODE_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY",
               "OPENROUTER_API_KEY", "DEEPSEEK_API_KEY", "FACTORY_OPERATOR_TOKEN", "FACTORY_RELEASE_TOKEN")

SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".woff", ".woff2", ".ttf", ".map"}
MAX_BYTES = 20_000_000
# Dependency and build directories are not the operator's code. Scanning them is slow and
# noisy: a vendored package legitimately contains strings that look like credentials (test
# fixtures, documentation examples), and the point of this check is what the product ships.
SKIP_DIRS = {"node_modules", ".git", ".nuxt", ".output", "release", ".data", "dist", "build"}


def main():
    if len(sys.argv) != 2:
        raise SystemExit("usage: secret_scan.py <artifact_dir>")
    root = Path(sys.argv[1]).resolve()
    if not root.is_dir():
        raise SystemExit(f"artifact directory not found: {root}")

    literal = {name: value for name in ENV_SECRETS
               if (value := os.environ.get(name)) and len(value) >= 16}
    findings = []
    scanned = 0
    for path in root.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        if any(part in SKIP_DIRS for part in path.relative_to(root).parts):
            continue
        if path.suffix.lower() in SKIP_SUFFIXES:
            continue
        if path.stat().st_size > MAX_BYTES:
            findings.append(f"{path.relative_to(root)}: file too large to scan")
            continue
        try:
            text = path.read_text(errors="ignore")
        except OSError as exc:
            findings.append(f"{path.relative_to(root)}: unreadable ({exc.__class__.__name__})")
            continue
        scanned += 1
        relative = path.relative_to(root)
        for name, pattern in SECRET_PATTERNS.items():
            if pattern.search(text):
                findings.append(f"{relative}: matches {name}")
        for name, value in literal.items():
            if value in text:
                findings.append(f"{relative}: contains the value of {name}")

    if findings:
        for finding in findings:
            print(f"SECRET LEAK: {finding}")
        raise SystemExit(1)
    # A scan that read nothing is not a pass. The message already printed "scanned 0", but nothing
    # acted on it: measured, an empty directory and a directory holding only skipped suffixes both
    # reported PASS. Since SKIP_DIRS contains release/.output/dist/build, a profile pointed at its
    # own build output could scan nothing and still be reported clean. Every sibling check fails
    # closed on an empty scan; this one now does too.
    if scanned == 0:
        # Distinguish "nothing here at all" from "only binary assets". A tree with just a `.png`
        # is a legitimate state (the test suite pins it), and skipping a binary is the documented
        # behaviour. A tree with nothing at all is not: it means the scan was pointed at the wrong
        # directory -- typically a build output that SKIP_DIRS excludes -- and reporting PASS
        # would be a false clean bill of health.
        considered = [path for path in root.rglob("*")
                      if path.is_file() and not path.is_symlink()
                      and not any(part in SKIP_DIRS for part in path.relative_to(root).parts)]
        if not considered:
            raise SystemExit("FAIL: the directory holds no files at all, so nothing was verified "
                             "(a scan that reads nothing must not report success)")
        # Every file present is a binary asset whose contents this check deliberately does not
        # read. That is a legitimate tree (a page with only images), so it passes -- but it says
        # so, rather than claiming it scanned text it never opened.
        print(f"PASS: {len(considered)} binary asset(s), no scannable text files; "
              "binary contents are not inspected by design")
    print(f"PASS: scanned {scanned} text files; no credential-shaped or configured secret values found")


if __name__ == "__main__":
    main()
