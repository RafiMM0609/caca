"""Operator-owned gate for the page's living layer: is the dealt motion actually mounted?

Why a second design gate, when `ui_lint.py` already exists: `ui_lint` reads CSS text and catches
mechanical defects (a `transition: all`, a missing focus ring). It cannot tell a page that has a
working moment from one that has a `<section data-moment>` and nothing else. Measured on the
Toko Roti rebuild, the best page the factory had ever produced scored `moment-none` from the
skill's own checker and 576 lines of hand-written CSS in place of the reviewed layer, and
nothing in the build refused to publish it.

**Static, on purpose.** The skill's `consistency.mjs` is the thorough answer and needs headless
Chrome: measured on this host, 106 s and 276 MB for two widths, 181-445 s for four, and it
reports 53 findings on an artifact that publishes today. That is the right tool for a design
review and the wrong tool for a gate that runs on every build: it would block every existing
product the moment it was enabled, and it costs more than the build it guards. This gate reads
files, runs in well under a second, and blocks only the defect it is named for.

It asserts exactly three things, each one measured to be a real failure mode:

  - A page that declares a moment must carry the vendored layer (its stylesheet is linked), or
    the moment is dead markup. Measured: `moments.js` injects a moment's CSS only after the
    module resolves, so without the static link a `photo-chapters` page stacks its photographs
    full-height on top of each other with scripts off.
  - A moment section must be a direct child of `<main>`, because the vendored CSS positions
    `main > [data-moment]`. Measured: inside a `<div>`, a `photo-chapters` section collapsed to
    133px tall and never became live.
  - A page must not re-implement scroll animation by hand beside a vendored layer. A scoped
    `@keyframes` used by a scroll-linked `IntersectionObserver` is the shape of that mistake.

Usage: python3 ui_moments.py <artifact_dir> [more dirs...]
Exit 1 on any finding, 1 when nothing was scanned, 0 when clean.
"""
import re
import sys
from pathlib import Path

PAGE_SUFFIXES = (".vue", ".html", ".svelte", ".jsx", ".tsx")
SKIP_DIRS = {"node_modules", ".git", ".nuxt", ".output", ".data", "dist", "build", "release",
             "public"}
MAX_BYTES = 2_000_000

# A moment declaration: `data-moment="photo-chapters"`, or Vue's bound form.
MOMENT_ATTR = re.compile(r"""data-moment\s*=\s*["']([a-z][a-z0-9-]*)["']""")
# The vendored stylesheets. A page may link the per-moment file or the bundle.
MOMENT_CSS = re.compile(r"""(?:moments/(?:bundle|[a-z0-9-]+/moment)\.css|assets/moments/)""")
MOTION_CSS = re.compile(r"""motion/motion\.css""")
# The layer that provides the hero entrance and the hover/press states.
MOTION_JS = re.compile(r"""motion/motion\.js""")
# A hand-written scroll animation: an observer or a scroll listener in the same file as its own
# keyframes. Either alone is legitimate; together they re-implement the layer.
SCROLL_DRIVER = re.compile(r"IntersectionObserver|addEventListener\(\s*['\"]scroll['\"]|"
                           r"onMounted\s*\([^)]*scroll", re.IGNORECASE)
KEYFRAMES = re.compile(r"@keyframes\s+[A-Za-z_][\w-]*")
# Animation on a property that cannot be composited is the thing that makes a page janky, and
# it is also the thing a hand-written scroll animation always ends up doing.
LAYOUT_ANIMATION = re.compile(r"(?:transition|animation)(?:-property)?\s*:\s*[^;{}]*\b"
                              r"(?:height|width|top|left|margin|padding|max-height)\b",
                              re.IGNORECASE)
# The moment's own class names, which its CSS and JS look for. A renamed one mounts nothing.
MOMENT_CLASSES = ("mo-ph", "mo-ph__list", "mo-ph__ch", "mo-wh__text", "mo-pc__", "mo-mm__")


def pages(root: Path):
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        if path.suffix.lower() not in PAGE_SUFFIXES:
            continue
        if any(part in SKIP_DIRS for part in path.relative_to(root).parts):
            continue
        try:
            if path.stat().st_size > MAX_BYTES:
                continue
        except OSError:
            continue
        yield path


class _Nesting:
    """Tracks which elements are open at the moment a `data-moment` attribute appears.

    Written as a forward pass with a stack rather than a regex, because the failure it detects
    (a moment nested in a wrapper `div`) is a statement about nesting, and regex-as-parser
    produced the class of false positive that teaches operators to ignore a gate.
    """

    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
            "param", "source", "track", "wbr"}
    # Elements that exist in the template but never in the rendered DOM, so they cannot be a
    # real parent. Measured false positive: a moment written as a direct child of `<main>` but
    # inside Vue's `<template v-if>` was reported as "inside <template>", while the browser
    # rendered its parent as MAIN. `<slot>` is the same kind of construct.
    TRANSPARENT = {"template", "slot", "component", "transition", "transition-group",
                   "keep-alive", "teleport", "suspense"}
    TAG = re.compile(r"<(/?)([A-Za-z][\w-]*)((?:[^>\"']|\"[^\"]*\"|'[^']*')*?)(/?)>", re.S)

    def __init__(self, text: str):
        self.text = self._strip_comments(text)
        self.stack: list[str] = []
        self.moment_parents: list[tuple[int, list[str]]] = []
        self._scan()

    @staticmethod
    def _strip_comments(text: str) -> str:
        out, index = [], 0
        while True:
            open_at = text.find("<!--", index)
            if open_at < 0:
                out.append(text[index:])
                return "".join(out)
            out.append(text[index:open_at])
            close_at = text.find("-->", open_at)
            if close_at < 0:
                return "".join(out)
            # Preserve line count so reported line numbers stay true.
            out.append("\n" * text.count("\n", open_at, close_at))
            index = close_at + 3

    def _scan(self) -> None:
        for match in self.TAG.finditer(self.text):
            closing, name, attrs, selfclose = match.group(1), match.group(2).lower(), match.group(3), match.group(4)
            if closing:
                if name in self.stack:
                    while self.stack and self.stack.pop() != name:
                        pass
                continue
            if "data-moment" in attrs and re.search(r'data-moment\s*=\s*["\']', attrs):
                # Report the nearest ancestor that will actually exist in the DOM.
                stack = [tag for tag in self.stack if tag not in self.TRANSPARENT]
                self.moment_parents.append((match.start(), stack))
            if selfclose or name in self.VOID:
                continue
            self.stack.append(name)


def scan_page(text: str, name: str) -> list[str]:
    """Findings for one template file. Pure, so the rules are testable without a tree."""
    findings = []
    moments = MOMENT_ATTR.findall(text)

    # 4. A layout property in a transition forces a reflow on every frame. Checked on every
    #    page, not only one with a moment: it is a motion defect either way, and a page that
    #    hand-writes its hover states has no `data-moment` to key on.
    for match in LAYOUT_ANIMATION.finditer(text):
        line = text.count("\n", 0, match.start()) + 1
        findings.append(
            f"{name}:{line}: animates a layout property (height/width/top/left/margin/padding). "
            f"Animate transform and opacity instead; a layout property repaints every frame and "
            f"is what makes motion feel heavy.")

    if not moments:
        return findings

    # 1. The vendored stylesheet must be linked somewhere the page can see it. A Nuxt app links
    #    it in nuxt.config.ts, so the search covers the file plus any sibling config.
    if not MOMENT_CSS.search(text):
        findings.append(
            f"{name}: declares moment {moments[0]!r} but links no vendored moment stylesheet. "
            f"The moment's CSS is what lays it out before its script runs; without it the "
            f"section is unstyled markup (a photo-chapters page stacks its photographs on top "
            f"of each other with scripts off). Link /assets/moments/bundle.css in <head>.")

    # 2. A moment section must be a direct child of <main>: the vendored CSS positions
    #    `main > [data-moment]`, and behind a wrapper the section never becomes live.
    nesting = _Nesting(text)
    for offset, stack in nesting.moment_parents:
        if not stack or stack[-1] != "main":
            line = text.count("\n", 0, offset) + 1
            where = "nothing" if not stack else f"<{stack[-1]}>"
            findings.append(
                f"{name}:{line}: a [data-moment] section sits directly inside {where}, not "
                f"<main>. The vendored moment CSS positions `main > [data-moment]`, so behind a "
                f"wrapper the section collapses or never goes live. Make <main> the section's "
                f"direct parent.")

    # 3. Motion must come from the vendored layer, not a second hand-written copy.
    if MOTION_CSS.search(text) and SCROLL_DRIVER.search(text) and KEYFRAMES.search(text):
        findings.append(
            f"{name}: drives animation from script (IntersectionObserver/scroll listener) "
            f"while also carrying its own @keyframes, beside the vendored motion layer. That "
            f"duplicates the layer and is the usual cause of a stuttering page. Use the dealt "
            f"moment for scroll motion and the layer for hover and entrance; remove the "
            f"hand-written one.")

    # 4. A layout property in a transition forces a reflow on every frame.
    for match in LAYOUT_ANIMATION.finditer(text):
        line = text.count("\n", 0, match.start()) + 1
        findings.append(
            f"{name}:{line}: animates a layout property (height/width/top/left/margin/padding). "
            f"Animate transform and opacity instead; a layout property repaints every frame and "
            f"is what makes motion feel heavy.")

    return findings


def main():
    if len(sys.argv) < 2:
        raise SystemExit("usage: ui_moments.py <artifact_dir> [more dirs...]")

    findings = []
    scanned = 0
    with_moments = 0
    for argument in sys.argv[1:]:
        root = Path(argument).resolve()
        if not root.is_dir():
            raise SystemExit(f"artifact directory not found: {root}")
        # A Nuxt app links its stylesheets in nuxt.config.ts, not in the page template, so the
        # search for the vendored layer has to include any config file beside the pages.
        config = ""
        for name in ("nuxt.config.ts", "nuxt.config.js", "vite.config.ts", "vite.config.js"):
            candidate = root / name
            if candidate.is_file():
                config += candidate.read_text(errors="ignore")
        for path in pages(root):
            scanned += 1
            try:
                text = path.read_text(errors="ignore")
            except OSError as exc:
                findings.append(f"{path.name}: unreadable ({exc.__class__.__name__})")
                continue
            label = path.relative_to(root).as_posix()
            if MOMENT_ATTR.search(text):
                with_moments += 1
            findings.extend(scan_page(text + config, label))

    if findings:
        for finding in findings[:60]:
            print(f"MOTION: {finding}")
        if len(findings) > 60:
            print(f"... and {len(findings) - 60} more")
        raise SystemExit(1)
    if scanned == 0:
        raise SystemExit("no page templates found; check the artifact path and layout")
    print(f"PASS: {scanned} page templates, {with_moments} declaring a dealt moment, "
          f"no motion findings")


if __name__ == "__main__":
    main()
