"""The dashboard must not overflow a phone.

Measured at 390px, `<html>` scrollWidth was 643 before this was fixed. The cause was not a fixed
width but unwrapped text: a `<p>` in the detail grid had clientWidth 256 and scrollWidth 750, so
the page grew to fit the longest planner question. A grid column refuses to shrink below its
content unless `minmax(0, 1fr)` is used, and long unbroken strings never wrap by default.

These are static assertions on the stylesheet, not a browser test: a real browser check would
need Playwright in the unit suite. They pin the specific rules that stopped the overflow, so a
future edit that removes one is visible here rather than on someone's phone.
"""
from pathlib import Path

CSS = (Path(__file__).resolve().parent.parent / "factory" / "static" / "style.css").read_text()


def test_the_detail_grid_can_shrink_below_its_content():
    """`1fr` alone still floors at min-content; `minmax(0, 1fr)` is what allows shrinking.

    Checked on the last rule that actually declares `grid-template-columns`, not on the last
    `.detail-grid` rule: a later rule may set only `gap`, and checking the string appears
    anywhere let a revert of the effective rule pass.
    """
    rules = [block.split("}")[0]
             for block in CSS.replace(" ", "").split(".detail-grid{")[1:]]
    declaring = [rule for rule in rules if "grid-template-columns:" in rule]
    assert declaring, "no .detail-grid rule declares grid-template-columns"
    final = declaring[-1]
    assert "minmax(0," in final, \
        f"the effective detail-grid column must be minmax(0, …), found {final!r}"


def test_long_strings_wrap_instead_of_widening_the_page():
    """Selectors, URLs, digests and planner prose are all unbroken strings."""
    assert "overflow-wrap:anywhere" in CSS
    for selector in ("#plan-questions", ".worker-node p", ".task ul", "#artifact-info code"):
        assert selector in CSS, f"{selector} must wrap rather than push the layout"


def test_a_planner_question_can_wrap():
    assert ".question label" in CSS
    assert "word-break:break-word" in CSS


def test_touch_targets_are_large_enough_on_a_phone():
    """12px font with 10px padding is under the 44px guidance."""
    mobile = CSS[CSS.index("@media(max-width:650px)"):]
    assert "min-height:44px" in mobile


def test_the_pipeline_wraps_on_a_phone():
    """Five columns of '01 Brief … 05 Review result' cannot be read at 390px."""
    mobile = CSS[CSS.index("@media(max-width:650px)"):]
    assert ".pipeline{display:grid" in mobile.replace(" ", "")


def test_text_inputs_use_a_size_that_does_not_zoom_ios():
    """Safari zooms the page when a focused input is under 16px."""
    mobile = CSS[CSS.index("@media(max-width:650px)"):]
    assert "font-size:16px" in mobile


def test_dialogs_fit_a_narrow_screen():
    mobile = CSS[CSS.index("@media(max-width:650px)"):]
    assert "calc(100vw - 24px)" in mobile.replace(" ", "") or "width:calc(100vw" in mobile.replace(" ", "")
