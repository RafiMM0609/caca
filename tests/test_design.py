"""A design preference that is not checked is not a rule.

Two layers are covered here: the static gate that refuses a mechanically bad artifact, and the
reviewer prompt section that catches what a regex cannot. Neither measures taste.
"""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from factory.engine import Engine

ROOT = Path(__file__).resolve().parent.parent
UI_LINT = ROOT / "checks" / "ui_lint.py"


def load_lint():
    spec = importlib.util.spec_from_file_location("ui_lint", UI_LINT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def lint(tmp_path, files, *extra):
    root = tmp_path / "artifact"
    root.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    return subprocess.run([sys.executable, str(UI_LINT), *extra, str(root)],
                          capture_output=True, text=True)


def test_a_clean_artifact_passes(tmp_path):
    result = lint(tmp_path, {
        "app.vue": "<template><p>Hello</p></template>\n<style>\n"
                   ".b { transition: transform 160ms cubic-bezier(0.23, 1, 0.32, 1); }\n"
                   ".b:focus-visible { outline: 2px solid currentColor; }\n</style>\n",
        "nuxt.config.ts": "export default {}\n",
    })
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS" in result.stdout


@pytest.mark.parametrize("css,fragment", [
    (".b { transition: all 200ms; }", "transition: all"),
    (".b { transition: opacity 200ms ease-in; }", "ease-in"),
    (".b { transition: opacity 200ms; }", "no timing function"),
    (".i:focus { outline: none; }", "outline: none"),
    (".t { user-select: none; }", "user-select: none"),
])
def test_each_mechanical_defect_is_caught(tmp_path, css, fragment):
    result = lint(tmp_path, {"app.vue": f"<template><p>Body text here</p></template>\n"
                                         f"<style>\n{css}\n</style>\n"})
    assert result.returncode == 1, f"{css} should be rejected"
    assert fragment in result.stdout


def test_a_removed_outline_with_a_focus_visible_replacement_is_accepted(tmp_path):
    """The rule targets `outline: none` *without* a replacement, not the property itself."""
    result = lint(tmp_path, {"app.vue":
                             "<template><p>Body text here</p></template>\n<style>\n"
                             ".i:focus { outline: none; }\n"
                             ".i:focus-visible { outline: 2px solid #111; }\n</style>\n"})
    assert result.returncode == 0, result.stdout


def test_a_missing_lang_attribute_is_caught(tmp_path):
    result = lint(tmp_path, {"index.html": "<!doctype html><html><body><p>x</p></body></html>\n"})
    assert result.returncode == 1
    assert "lang" in result.stdout


def test_a_declared_lang_attribute_is_accepted(tmp_path):
    result = lint(tmp_path, {"index.html":
                             '<!doctype html><html lang="id"><body><p>x</p></body></html>\n'})
    assert result.returncode == 0, result.stdout


def test_an_empty_artifact_is_not_a_pass(tmp_path):
    """Nothing scanned usually means the path is wrong, so it must not report success."""
    result = lint(tmp_path, {"README.md": "# nothing to lint\n"})
    assert result.returncode == 1
    assert "no stylesheets" in result.stderr


def test_dependency_directories_are_skipped(tmp_path):
    """A vendored package is not the product's design to fix."""
    result = lint(tmp_path, {
        "node_modules/pkg/bad.css": ".x { transition: all 1s; }\n",
        "app.vue": "<template><p>Body text here</p></template>\n<style>\n.a { color: red; }\n</style>\n",
    })
    assert result.returncode == 0, result.stdout


def test_the_studio_profile_runs_the_design_gate():
    data = json.loads((ROOT / "examples" / "config-studio-linux.json").read_text())
    checks = [" ".join(argv) for argv in data["profiles"]["nuxt-studio"]["checks_argv"]]
    assert any("ui_lint" in line for line in checks), "the design gate must be wired"
    assert any("secret_scan" in line for line in checks), "the secret gate must stay"


def test_the_reviewer_is_given_design_defects_as_rules():
    """The reviewer has no tools, so its rules must be inline rather than a skill file."""
    source = Path(Engine.__module__.replace(".", "/") + ".py")
    text = (ROOT / source).read_text()
    assert "review it as design" in text
    for rule in ("transition: all", "ease-in", ":focus-visible", "360px", "user-select: none"):
        assert rule in text, f"{rule} must be named as a defect"
    # The prompt is a concatenation of string literals, so match across the join.
    assert "do not reject one merely because a " in text or \
        "do not reject one merely because a layout differs" in text, \
        "the reviewer must not reject on taste"
