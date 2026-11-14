"""The operator's type scale, asserted against the stylesheet it actually ships.

The sizes below are accessibility floors, not taste: body text under 16px strains a laptop
screen, and a focused form field under 16px makes iOS zoom the page. They were requested as a
table, so they are pinned as a table — a future restyle that drops `body` back to 14px should
fail here rather than be discovered by reading a screenshot.
"""
import re
from pathlib import Path

CSS = (Path(__file__).resolve().parent.parent / "factory" / "static" / "style.css").read_text()

# element -> (min_px, max_px) from the operator's table.
SCALE = {
    "body": (16, 16),
    "button": (16, 16),
    "h1": (28, 32),
    "h2": (24, 24),
    "h3": (20, 20),
    "h4": (18, 18),
}


def token(value):
    """Resolve a `font-size` value to pixels, following a `--token` when one is used."""
    match = re.fullmatch(r"var\((--[a-z0-9-]+)\)", value.strip())
    if not match:
        return float(value.strip().removesuffix("px"))
    declared = re.search(re.escape(match.group(1)) + r"\s*:\s*([0-9.]+)px", CSS)
    assert declared, f"{match.group(1)} is used but never declared"
    return float(declared.group(1))


def size_of(selector):
    """The last declaration of `font-size` for an exact selector at the top level."""
    found = re.findall(r"(?m)^" + re.escape(selector) + r"\{([^}]*)\}", CSS)
    assert found, f"{selector} has no rule at all"
    values = [re.search(r"font-size:([^;}]+)", block) for block in found]
    values = [match.group(1) for match in values if match]
    assert values, f"{selector} declares no font-size"
    return token(values[-1])


def test_the_scale_tokens_exist():
    """The scale is declared once so a size is a decision, not forty scattered literals."""
    for name in ("--text-body", "--text-secondary", "--text-caption", "--text-button",
                 "--h1", "--h2", "--h3", "--h4"):
        assert re.search(re.escape(name) + r"\s*:", CSS), f"{name} is not declared"


def test_headings_match_the_requested_table():
    for element, (low, high) in SCALE.items():
        if not element.startswith("h"):
            continue
        actual = size_of(element)
        assert low <= actual <= high, f"{element} is {actual}px, expected {low}-{high}px"


def test_body_text_is_at_least_sixteen_pixels():
    assert size_of("body") >= 16


def test_buttons_are_sixteen_pixels():
    """`font:inherit` made a button 10px inside a dense row and 12px inside a heading."""
    assert size_of("button") >= 16


def test_labels_and_secondary_text_are_fourteen_pixels():
    assert size_of("label") >= 14


def test_captions_are_twelve_to_thirteen_pixels():
    """The eyebrow/nav-label tier. Nothing in the UI may be smaller than this."""
    caption = token(re.search(r"--text-caption:\s*([0-9.]+px)", CSS).group(1))
    assert 12 <= caption <= 13


def test_no_text_is_smaller_than_twelve_pixels():
    """The old sheet had sixteen 10px rules and an 8px and a 7px. Caption is the floor now."""
    sizes = [token(value) for value in re.findall(r"font-size:(var\(--[a-z0-9-]+\)|[0-9.]+px)", CSS)]
    assert sizes, "no font sizes found"
    assert min(sizes) >= 12, f"text below the 12px floor: {sorted(set(s for s in sizes if s < 12))}"


def test_the_stylesheet_uses_the_tokens_rather_than_literals():
    """Literals are how the scale drifted; only the mobile H1 step is allowed to be one."""
    literals = re.findall(r"font-size:([0-9.]+px)", CSS)
    assert len(literals) <= 1, f"font sizes should come from tokens, found literals: {literals}"
