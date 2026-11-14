"""The review prompt must not send design rules to a backend task.

Measured on a live build: review was 37% of wall clock (454s of 1219s), and `json-storage` --
2 files, no CSS -- spent 144s in review against 114s in the worker that wrote it. The prompt was
2,367 characters, of which 1,598 (68%) were design rules about `ease-in`, `outline: none` and
`transition: all` that a storage module cannot violate.

The rules are kept verbatim for interface work: of 59 reviewed workers only 3 were rejected, but
they were real defects (a Crash/DoS slice, a data-loss path, a request race), so the gate is
worth its cost where it applies. This change makes *whether the design section is sent*
conditional, not whether review happens at all.
"""
from factory.engine import INTERFACE_SUFFIXES, interface_changes


def test_a_backend_change_gets_no_design_rules():
    changed = ["src/main.zig", "src/store.zig", "src/router.zig", "Cargo.toml"]
    assert interface_changes(changed) == []


def test_a_page_change_gets_the_design_rules():
    for name in ("public/styles.css", "app/page.html", "components/Button.vue",
                 "src/index.jsx", "layout.svelte", "site/app.scss"):
        assert interface_changes([name]) == [name], name


def test_a_mixed_change_is_treated_as_interface_work():
    """One template file is enough: the rules must not be skipped because most files are code."""
    changed = ["src/main.zig", "views/page.html", "src/store.zig"]
    assert interface_changes(changed) == ["views/page.html"]


def test_every_declared_suffix_is_matched():
    for suffix in INTERFACE_SUFFIXES:
        assert interface_changes(["file" + suffix]), suffix
        assert interface_changes(["file" + suffix.upper()]), "matching must be case-insensitive"


def test_an_empty_diff_is_not_interface_work():
    assert interface_changes([]) == []
    assert interface_changes(None) == []


def test_a_file_that_merely_mentions_css_is_not_interface_work():
    """The check is on the extension, not on a substring, so `styles.css.md` is not a style file."""
    assert interface_changes(["docs/styles.css.md"]) == []
    #  is a server module;  is a component. The distinction matters because the
    # design rules would otherwise be sent for a backend file.
    assert interface_changes(["src/css.ts"]) == []
    assert interface_changes(["src/page.tsx"]) == ["src/page.tsx"]
