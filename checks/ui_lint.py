"""Operator-owned design gate for a built artifact.

A design *preference* that is not checked is not a rule. A skill raises the floor of what a
worker produces, but nothing forces the result through a gate, so a sloppy artifact still
publishes as long as its behaviour is correct. This check is the gate for the mechanical
subset: the mistakes that are visible in the text of the CSS and that a reviewer reading a
diff can plausibly miss.

Deliberately narrow. It claims only what a static scan can prove:

  - `transition: all` — animates properties nobody chose, and is the single most common
    cause of janky, unintentional motion.
  - `ease-in` on an entrance — decelerating motion reads as responsive; accelerating motion
    reads as hesitant. This is the mistake the skills exist to prevent.
  - `ease` / `linear` / a bare duration on a hover or entrance transition — a default curve
    is a choice nobody made.
  - `outline: none` without a `:focus-visible` replacement — removes the keyboard user's only
    position indicator.
  - `user-select: none` on body copy — makes text unselectable.
  - a missing `lang` attribute on `<html>` — breaks screen-reader pronunciation.

It does NOT judge taste, spacing, hierarchy or colour. Those need a human or an LLM, and a
regex that pretends otherwise would produce false failures that teach operators to ignore it.

Usage: python3 ui_lint.py <artifact_dir> [more dirs...]
Exit 1 on any finding, 1 when nothing was scanned (a bad path is not a pass), 0 when clean.
"""
import re
import sys
from pathlib import Path

STYLE_SUFFIXES = (".css", ".vue", ".html", ".svelte", ".jsx", ".tsx")
SKIP_DIRS = {"node_modules", ".git", ".nuxt", ".output", ".data", "dist", "build", "release"}
MAX_BYTES = 2_000_000

# `transition: all` in any position, allowing for interpolation and vendor prefixes.
TRANSITION_ALL = re.compile(r"transition(?:-property)?\s*:\s*[^;{}]*\ball\b", re.IGNORECASE)
# An ease-in curve applied to an entrance-ish transition.
EASE_IN = re.compile(
    r"transition(?:-timing-function)?\s*:\s*[^;{}]*\b(?:ease-in|cubic-bezier\(\s*0?\.\d+\s*,"
    r"\s*0?\.\d+\s*,\s*1\s*,\s*0?\.\d+\s*\))", re.IGNORECASE)
OUTLINE_NONE = re.compile(r"outline\s*:\s*(?:none|0)\b", re.IGNORECASE)
# A replacement ring counts only when a focus rule actually sets an outline. Testing this with
# one regex over the whole file was wrong twice: a bare `:focus` matched the very rule that
# removed the ring, and a negative lookahead after `\s*` backtracked into acceptance. The rules
# are read block by block instead, which is unambiguous and can be tested directly.
FOCUS_RULE = re.compile(r"([^{}]*:focus(?:-visible)?[^{}]*)\{([^}]*)\}", re.IGNORECASE)
OUTLINE_DECL = re.compile(r"\boutline\s*:\s*([^;}]+)", re.IGNORECASE)
USER_SELECT_NONE = re.compile(r"user-select\s*:\s*none", re.IGNORECASE)
TRANSITION_SHORTHAND = re.compile(r"transition\s*:\s*([^;{}]+)", re.IGNORECASE)
HTML_TAG = re.compile(r"<html\b([^>]*)>", re.IGNORECASE)
LANG_ATTR = re.compile(r"\blang\s*=", re.IGNORECASE)
# Elements/text where unselectable content is a real accessibility defect.
BODY_TEXT_HINT = re.compile(r"<(?:p|article|main|li|td|blockquote)\b", re.IGNORECASE)


def candidates(root: Path):
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        if path.suffix.lower() not in STYLE_SUFFIXES:
            continue
        if any(part in SKIP_DIRS for part in path.relative_to(root).parts):
            continue
        if path.stat().st_size > MAX_BYTES:
            continue
        yield path


def has_focus_ring(text: str) -> bool:
    """True when some focus rule actually draws an outline, rather than removing one.

    The value is inspected directly instead of trusted to a lookahead: `outline: none` must not
    count as a ring, and a `(?!none)` after `\\s*` accepts it through backtracking.
    """
    for match in FOCUS_RULE.finditer(text):
        for declaration in OUTLINE_DECL.finditer(match.group(2)):
            value = declaration.group(1).strip().lower()
            if value and value not in ("none", "0", "0px"):
                return True
    return False


def scan_text(text: str, name: str) -> list[str]:
    """Findings for one file. Pure, so the rules can be tested without a filesystem."""
    findings = []

    for match in TRANSITION_ALL.finditer(text):
        line = text.count("\n", 0, match.start()) + 1
        findings.append(f"{name}:{line}: `transition: all` animates properties nobody chose; "
                        f"name them explicitly")

    if EASE_IN.search(text):
        line = text.count("\n", 0, EASE_IN.search(text).start()) + 1
        findings.append(f"{name}:{line}: `ease-in` on a transition reads as hesitant; use "
                        f"`ease-out` or a custom decelerating curve for entrances")

    if OUTLINE_NONE.search(text) and not has_focus_ring(text):
        line = text.count("\n", 0, OUTLINE_NONE.search(text).start()) + 1
        findings.append(f"{name}:{line}: `outline: none` with no `:focus-visible` replacement "
                        f"removes the keyboard user's position indicator")

    if USER_SELECT_NONE.search(text) and BODY_TEXT_HINT.search(text):
        line = text.count("\n", 0, USER_SELECT_NONE.search(text).start()) + 1
        findings.append(f"{name}:{line}: `user-select: none` with body text present makes "
                        f"content unselectable")

    # A transition with no timing function at all: a bare duration is a choice nobody made.
    for match in TRANSITION_SHORTHAND.finditer(text):
        value = match.group(1)
        has_duration = re.search(r"\d+(?:\.\d+)?m?s\b", value)
        has_curve = re.search(r"\b(?:linear|ease|ease-in|ease-out|ease-in-out|step|"
                              r"cubic-bezier|var\()", value, re.IGNORECASE)
        if has_duration and not has_curve and "none" not in value.lower():
            line = text.count("\n", 0, match.start()) + 1
            findings.append(f"{name}:{line}: transition has a duration but no timing function; "
                            f"a default curve is a choice nobody made")

    return findings


def scan_markup(text: str, name: str) -> list[str]:
    """Markup-level findings, kept separate because they do not apply to a .css file."""
    findings = []
    for match in HTML_TAG.finditer(text):
        if not LANG_ATTR.search(match.group(1)):
            line = text.count("\n", 0, match.start()) + 1
            findings.append(f"{name}:{line}: <html> has no `lang` attribute; screen readers "
                            f"cannot choose the right pronunciation")
    return findings


def main():
    if len(sys.argv) < 2:
        raise SystemExit("usage: ui_lint.py <artifact_dir> [more dirs...]")

    findings = []
    scanned = 0
    for argument in sys.argv[1:]:
        root = Path(argument).resolve()
        if not root.is_dir():
            raise SystemExit(f"artifact directory not found: {root}")
        for path in candidates(root):
            scanned += 1
            try:
                text = path.read_text(errors="ignore")
            except OSError as exc:
                findings.append(f"{path.name}: unreadable ({exc.__class__.__name__})")
                continue
            name = path.relative_to(root).as_posix()
            findings.extend(scan_text(text, name))
            if path.suffix.lower() in (".vue", ".html", ".svelte", ".jsx", ".tsx"):
                findings.extend(scan_markup(text, name))

    if findings:
        for finding in findings[:60]:
            print(f"DESIGN: {finding}")
        if len(findings) > 60:
            print(f"... and {len(findings) - 60} more")
        raise SystemExit(1)
    if scanned == 0:
        # Nothing scanned is not a clean artifact; it usually means the path is wrong. A gate
        # that cannot see anything must not report success.
        raise SystemExit("no stylesheets or templates found; check the artifact path and layout")
    print(f"PASS: {scanned} style/template files, no mechanical design findings")


if __name__ == "__main__":
    main()
