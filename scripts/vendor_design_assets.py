"""Vendor the living-UI assets a page needs into the Nuxt starter, reproducibly.

Three families of asset, for three different reasons:

**Moments and the motion layer** (`assets/moments/`, `assets/motion/`). Runtime ES modules and
CSS that the page loads as plain files. They must sit in `public/` and must NOT be bundled:
`moments.js` resolves each moment with `new URL('<id>/moment.js', import.meta.url)`, so Vite's
`/_nuxt/` chunking breaks every relative path. Measured before this was fixed: both moments
failed with `Failed to fetch dynamically imported module` and the checker reported
`moment-none`. Copying them as static files is the fix, and it is also why this script exists
instead of the worker hand-copying them per project.

**Fonts** (`assets/fonts/`). The skill's dealer picks any of 39 type pairings, which name 49
Google font families. A page that names Google Fonts in a `<link>` needs the network at render
time, in the client's browser, on every visit, and fails on an offline host. All 49 families
are self-hosted here as `latin` woff2 at ~1.4 MB total on disk; a page downloads only the one
or two families its `@font-face` rules point at, which is verified by
`tests/test_design_assets.py`.

**The dealer tables** are NOT copied. `factory/design.py` reads them from `skills/` at deal
time, so upgrading the skill changes the offer without a second copy drifting from it.

Usage:
    python3 scripts/vendor_design_assets.py [--skill-dir skills] [--out <starter>/public/assets]

Re-run it after upgrading the vendored skill. It deletes and rewrites only the directories it
owns (`moments`, `motion`, `fonts`), so an operator's own files beside them survive.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import urllib.parse
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEFAULT_SKILL = REPO / "skills" / "design-taste-frontend"
DEFAULT_OUT = REPO / "factory" / "starters" / "nuxt-studio" / "public" / "assets"

# Fetched with a desktop UA so Google returns woff2 rather than legacy ttf.
FONT_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
           "Chrome/120.0.0.0 Safari/537.36")
FONT_CSS = "https://fonts.googleapis.com/css2?family={family}:wght@400..600&display=swap"
# Some dealer families are not variable fonts (DM Mono, IBM Plex Mono, Instrument Serif, Zilla
# Slab), and Google answers a range request for those with HTTP 400 rather than the nearest
# weights. Asking for the two weights the skill actually uses is the fallback that works.
FONT_CSS_STATIC = "https://fonts.googleapis.com/css2?family={family}:wght@400;500&display=swap"

# Moment files the runtime reads. `_demo/`, `gallery.html` and the demo pages are the skill's
# own showcase (2 MB of sample photographs) and are never loaded by a page.
MOMENT_RUNTIME = ("moment.js", "moment.css", "meta.json")


def log(message: str) -> None:
    print(message, flush=True)


def copy_moments(skill: Path, out: Path) -> int:
    """Copy the moment loader, the shared library, and every dealt moment's runtime files.

    Also writes `moments/bundle.css`: every moment's CSS concatenated into the one file the
    page links statically. Why that copy exists, measured: `moments.js` injects each moment's
    stylesheet only after its module resolves, so with JavaScript disabled or still loading
    the markup is unstyled. For `photo-chapters` that is not cosmetic - its chapters are
    stacked by that CSS, so an unstyled page showed all three photographs full-height on top
    of each other (a 5636px document with every caption buried). Linking the CSS in `<head>`
    fixes it for whichever moment is dealt, including a moment this project did not choose.
    """
    source = skill / "assets" / "moments"
    if not source.is_dir():
        raise SystemExit(f"moments directory not found: {source}")
    target = out / "moments"
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)

    for shared in ("moments.js", "lib.js", "index.json"):
        shutil.copyfile(source / shared, target / shared)

    count = 0
    stylesheets: list[str] = []
    # Only the moments the skill still deals. The index marks retired ones (`closing-lift`,
    # `headline-arrives`, `lines-rise`, `window-parallax`, `word-rotate`); shipping their CSS
    # would add bytes for a moment the dealer can never pick, and a retired moment is one the
    # skill's own checker rejects.
    index = json.loads((source / "index.json").read_text())
    dealt = {m["id"] for m in index.get("moments", []) if not m.get("retired")}
    for folder in sorted(p for p in source.iterdir() if p.is_dir()):
        if folder.name.startswith("_") or folder.name not in dealt:
            continue
        files = [folder / name for name in MOMENT_RUNTIME if (folder / name).is_file()]
        if not files:
            continue
        (target / folder.name).mkdir()
        for item in files:
            shutil.copyfile(item, target / folder.name / item.name)
        sheet = folder / "moment.css"
        if sheet.is_file():
            stylesheets.append(f"/* {folder.name} */\n" + sheet.read_text().rstrip() + "\n")
        count += 1

    (target / "bundle.css").write_text(
        "/* Every dealt moment's CSS, in one file the page links in <head>.\n"
        "   Generated by scripts/vendor_design_assets.py - do not edit by hand.\n"
        "   Linked statically so a moment is laid out correctly before (and without) its JS. */\n"
        + "\n".join(stylesheets))
    return count


def copy_motion(skill: Path, out: Path) -> None:
    """Copy the micro-interaction layer (hover, press, hero entrance) and nothing else."""
    source = skill / "assets" / "motion"
    if not source.is_dir():
        raise SystemExit(f"motion directory not found: {source}")
    target = out / "motion"
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    for name in ("motion.css", "motion.js", "meta.json"):
        if (source / name).is_file():
            shutil.copyfile(source / name, target / name)


def dealer_families(skill: Path) -> list[str]:
    """Every font family the type dealer can name, in a stable order."""
    table = json.loads((skill / "library" / "type.json").read_text())
    families: set[str] = set()
    for entry in table:
        for role in ("display", "text", "mono"):
            value = entry.get(role)
            if isinstance(value, dict) and value.get("family"):
                for part in str(value["family"]).split("/"):
                    families.add(part.strip())
    return sorted(f for f in families if f)


def _get(url: str, timeout: int = 30) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": FONT_UA})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def fetch_fonts(skill: Path, out: Path, cache: Path) -> tuple[int, list[str]]:
    """Download the latin woff2 of every dealer family and write one `fonts.css`.

    Only the `latin` subset is taken. The skill's pages are written in one language per page,
    and Google splits `latin-ext` into a second file that a page using accented characters
    still loads through `unicode-range`; shipping both doubles the disk for a subset most
    builds never request. `latin` covers ASCII plus the punctuation the skill's copy patterns
    use. A page that genuinely needs more can be given `--subsets` later; guessing wider now
    would cost every build for a case none has hit.
    """
    target = out / "fonts"
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    cache.mkdir(parents=True, exist_ok=True)

    faces: list[str] = []
    failures: list[str] = []
    for family in dealer_families(skill):
        slug = re.sub(r"[^a-z0-9]+", "-", family.lower()).strip("-")
        cached = cache / f"{slug}.woff2"
        payload: bytes | None = None
        if cached.is_file():
            payload = cached.read_bytes()
        else:
            css = ""
            for template in (FONT_CSS, FONT_CSS_STATIC):
                try:
                    css = _get(template.format(family=urllib.parse.quote(family))).decode()
                    break
                except Exception as exc:  # HTTP 400 for a non-variable family, or no network
                    last = f"{family}: css ({exc.__class__.__name__})"
            if not css:
                failures.append(last)
                continue
            match = None
            for subset, body in re.findall(r"/\* (\S+) \*/\s*@font-face \{(.*?)\}", css, re.S):
                if subset == "latin":
                    match = re.search(r"url\((https[^)]+)\)", body)
                    break
            if not match:
                failures.append(f"{family}: no latin subset offered")
                continue
            try:
                payload = _get(match.group(1))
            except Exception as exc:
                failures.append(f"{family}: woff2 ({exc.__class__.__name__})")
                continue
            cached.write_bytes(payload)
        target_file = target / f"{slug}.woff2"
        target_file.write_bytes(payload)
        faces.append(
            "@font-face{font-family:'%s';font-style:normal;font-weight:400 600;"
            "font-display:swap;src:url('/assets/fonts/%s.woff2') format('woff2');"
            "unicode-range:U+0000-00FF,U+0131,U+0152-0153,U+02BB-02BC,U+02C6,U+02DA,U+02DC,"
            "U+0304,U+0308,U+0329,U+2000-206F,U+20AC,U+2122,U+2191,U+2193,U+2212,U+2215,"
            "U+FEFF,U+FFFD}" % (family, slug)
        )
    (target / "fonts.css").write_text(
        "/* Self-hosted from Google Fonts (SIL Open Font License 1.1), latin subset.\n"
        "   Generated by scripts/vendor_design_assets.py - do not edit by hand. */\n"
        + "\n".join(faces) + "\n")
    return len(faces), failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skill-dir", default=str(DEFAULT_SKILL))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--cache", default=str(REPO / ".cache" / "fonts"),
                        help="where downloaded woff2 files are kept between runs")
    parser.add_argument("--skip-fonts", action="store_true",
                        help="copy moments and motion only, keep the existing fonts")
    args = parser.parse_args()

    skill, out = Path(args.skill_dir).resolve(), Path(args.out).resolve()
    if not skill.is_dir():
        raise SystemExit(f"vendored skill not found: {skill}")
    out.mkdir(parents=True, exist_ok=True)

    moments = copy_moments(skill, out)
    log(f"moments: {moments} deals + loader")
    copy_motion(skill, out)
    log("motion: motion.css, motion.js")

    if not args.skip_fonts:
        faces, failures = fetch_fonts(skill, out, Path(args.cache))
        log(f"fonts: {faces} families self-hosted")
        if failures:
            # A missing family is not fatal: the dealer can still pick another, and a build
            # that silently lost a font would ship a page in the wrong typeface.
            log("fonts not vendored (dealer will avoid them only if the CSS is absent):")
            for failure in failures:
                log(f"  - {failure}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
