"""The living layer: the dealer, the vendored assets, and the gate that enforces them.

Two things are tested here that are easy to get wrong and expensive to discover late:

  - **The dealer is a lookup, not a judgement.** It must be deterministic per project, vary
    between projects, respect the skill's own per-kind rules (no shader on a cafe), and never
    deal a place's palette to a software page.
  - **The gate catches the three measured failure modes** and stays quiet on a correct page.
    A gate that fires on a good page teaches operators to ignore it; one that misses the
    wrapper bug lets a collapsed section publish.
"""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from factory.design import (DesignDealer, deal_blocks, detect_kind, detect_trade,
                            PAGE_KINDS)

ROOT = Path(__file__).resolve().parent.parent
SKILLS = ROOT / "skills"
GATE = ROOT / "checks" / "ui_moments.py"
STARTER = ROOT / "factory" / "starters" / "nuxt-studio"


def load_gate():
    spec = importlib.util.spec_from_file_location("ui_moments", GATE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_gate(tmp_path, files):
    root = tmp_path / "artifact"
    root.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    return subprocess.run([sys.executable, str(GATE), str(root)],
                          capture_output=True, text=True)


# -- the dealer -------------------------------------------------------------------------

def test_the_dealer_reads_the_vendored_tables():
    """It must use the skill's own library, not a second copy that can drift from it."""
    dealer = DesignDealer(SKILLS)
    assert dealer.available()
    assert len(dealer.palettes) > 20
    assert len(dealer.types) > 10
    assert dealer.moments, "the moment index must load"
    assert all(not m.get("retired") for m in dealer.moments), \
        "a retired moment must never be dealt"


def test_the_dealer_is_deterministic_per_project():
    """A rebuild must not silently change a project's design."""
    dealer = DesignDealer(SKILLS)
    first = dealer.deal("Toko roti rumahan di Jalan Melati", seed="project-a")
    again = dealer.deal("Toko roti rumahan di Jalan Melati", seed="project-a")
    assert first.as_dict() == again.as_dict()


def test_two_projects_in_one_trade_do_not_get_one_design():
    """The dealer keeps a pool, so every bakery client is not the same bakery."""
    dealer = DesignDealer(SKILLS)
    palettes = {dealer.deal("toko roti", seed=f"p{i}").palette.get("id") for i in range(10)}
    assert len(palettes) > 1, "one palette for every project in a trade is a template"


@pytest.mark.parametrize("brief,expected", [
    ("Toko roti rumahan, jual roti tawar dan croissant", "place"),
    ("Landing page SaaS untuk manajemen proyek", "software"),
    ("Klinik gigi keluarga di Bandung", "service"),
    ("Game petualangan 2D bertema nusantara", "gaming"),
])
def test_the_kind_is_read_from_the_brief(brief, expected):
    assert detect_kind(brief) == expected


def test_a_declared_kind_beats_a_guess():
    assert detect_kind("aplikasi kasir", declared="place") == "place"
    assert detect_kind("anything", declared="not-a-kind") in PAGE_KINDS


def test_a_software_page_is_not_dealt_a_places_palette():
    """Measured defect: a POS brief naming "warung sate" was dealt the restaurant palette.

    Right for a warung, wrong for a cashier application, which is a software product.
    """
    dealer = DesignDealer(SKILLS)
    deal = dealer.deal("Aplikasi kasir (POS) untuk warung sate", seed="pos")
    assert deal.page_kind == "software"
    assert deal.trade not in ("restaurant", "bakery", "cafe", "hotel", "retail")
    assert detect_trade("Aplikasi kasir untuk warung sate", "software") == "saas"


@pytest.mark.parametrize("kind", ["place", "product", "service"])
def test_a_non_technical_page_is_never_dealt_webgl(kind):
    """House rule 8: a cafe, bakery or shop gets photographs, never a shader."""
    dealer = DesignDealer(SKILLS)
    for seed in ("a", "b", "c", "d"):
        for moment in dealer.deal("x y z", seed=seed, page_kind=kind).moments:
            assert int(moment.get("webgl") or 0) == 0, f"{moment['id']} uses WebGL on {kind}"


def test_every_kind_can_be_dealt_a_moment():
    """The scope decision: all page kinds, not just the photographic ones."""
    dealer = DesignDealer(SKILLS)
    for kind in PAGE_KINDS:
        deal = dealer.deal("a brief with photographs", seed="k", page_kind=kind)
        assert deal.moments, f"{kind} was dealt no moment"
        assert len(deal.moments) == 1, "one moment per page: two is a fault on a short page"


def test_a_moment_that_needs_a_photograph_is_skipped_without_one():
    dealer = DesignDealer(SKILLS)
    deal = dealer.deal("a brief", seed="k", page_kind="place", has_photo=False)
    for moment in deal.moments:
        assert not (moment.get("needs") or {}).get("photo")


def test_a_missing_skill_degrades_instead_of_failing(tmp_path):
    """A build must not die because the vendored skill is absent."""
    dealer = DesignDealer(tmp_path)
    assert not dealer.available()
    deal = dealer.deal("toko roti", seed="s")
    assert deal.palette == {} and deal.moments == []
    assert any("missing" in note for note in deal.notes)
    # And the block is still readable, so the worker is told what is going on.
    assert "DEALT DESIGN" in deal_blocks(deal)


def test_the_dealt_block_tells_the_worker_not_to_invent_motion():
    dealer = DesignDealer(SKILLS)
    block = deal_blocks(dealer.deal("toko roti di Bandung", seed="s"))
    assert "data-moment=" in block
    assert "do not write your own scroll" in block.lower()
    assert "--p-bg:" in block, "the palette must be given as tokens the page sets"


# -- the gate ---------------------------------------------------------------------------

def test_a_page_with_no_moment_passes_and_is_still_scanned(tmp_path):
    """A page that shows nothing must not be reported clean; a page with no moment is clean."""
    result = run_gate(tmp_path, {"pages/index.vue": "<template><main><p>Hi</p></main></template>\n"})
    assert result.returncode == 0, result.stdout
    assert "no motion findings" in result.stdout


def test_a_moment_without_the_vendored_stylesheet_is_caught(tmp_path):
    """Without the static link a photo-chapters page stacks its photographs on each other."""
    result = run_gate(tmp_path, {
        "pages/index.vue":
            '<template><main><section data-moment="photo-chapters">x</section></main></template>\n',
    })
    assert result.returncode == 1
    assert "links no vendored moment stylesheet" in result.stdout


def test_the_stylesheet_may_be_linked_in_the_nuxt_config(tmp_path):
    result = run_gate(tmp_path, {
        "nuxt.config.ts": "export default { app: { head: { link: [{ href: '/assets/moments/bundle.css' }] } } }\n",
        "pages/index.vue":
            '<template><main><section data-moment="photo-chapters">x</section></main></template>\n',
    })
    assert result.returncode == 0, result.stdout


def test_a_moment_behind_a_wrapper_is_caught(tmp_path):
    """Measured: inside a div, a photo-chapters section collapsed to 133px and never went live."""
    result = run_gate(tmp_path, {
        "nuxt.config.ts": "head: '/assets/moments/bundle.css'\n",
        "pages/index.vue":
            '<template><div><section data-moment="photo-chapters">x</section></div></template>\n',
    })
    assert result.returncode == 1
    assert "not <main>" in result.stdout


def test_a_commented_out_main_is_not_a_parent(tmp_path):
    """The gate reads nesting, not text; a comment must not satisfy it."""
    result = run_gate(tmp_path, {
        "nuxt.config.ts": "head: '/assets/moments/bundle.css'\n",
        "pages/index.vue":
            '<template><div><!-- <main> --><section data-moment="word-highlight">x'
            '</section></div></template>\n',
    })
    assert result.returncode == 1
    assert "not <main>" in result.stdout


def test_a_hand_written_scroll_animation_beside_the_layer_is_caught(tmp_path):
    result = run_gate(tmp_path, {
        "pages/index.vue":
            '<template><main><section data-moment="word-highlight">x</section></main></template>\n'
            '<style>\n@keyframes rise { from { opacity: 0 } }\n</style>\n'
            '<script setup>onMounted(() => new IntersectionObserver(() => {}).observe(document.body))</script>\n',
        "nuxt.config.ts": "head: '/assets/moments/bundle.css' '/assets/motion/motion.css'\n",
    })
    assert result.returncode == 1
    assert "duplicates the layer" in result.stdout


def test_animating_a_layout_property_is_caught(tmp_path):
    result = run_gate(tmp_path, {
        "pages/index.vue": "<template><main><p>x</p></main></template>\n"
                           "<style>.a { transition: height 200ms ease; }</style>\n",
    })
    assert result.returncode == 1
    assert "layout property" in result.stdout


def test_animating_transform_is_accepted(tmp_path):
    result = run_gate(tmp_path, {
        "pages/index.vue": "<template><main><p>x</p></main></template>\n"
                           "<style>.a { transition: transform 160ms cubic-bezier(0,0,.2,1); }</style>\n",
    })
    assert result.returncode == 0, result.stdout


def test_an_empty_artifact_is_not_a_pass(tmp_path):
    result = run_gate(tmp_path, {"README.md": "nothing\n"})
    assert result.returncode == 1
    assert "no page templates" in result.stderr


def test_dependency_directories_are_skipped(tmp_path):
    """A vendored package is not the product's design to fix."""
    result = run_gate(tmp_path, {
        "node_modules/pkg/x.vue": '<template><div><section data-moment="x">y</section></div></template>\n',
        "pages/index.vue": "<template><main><p>x</p></main></template>\n",
    })
    assert result.returncode == 0, result.stdout


def test_the_studio_profile_runs_the_motion_gate():
    data = json.loads((ROOT / "examples" / "config-studio-linux.json").read_text())
    checks = [" ".join(argv) for argv in data["profiles"]["nuxt-studio"]["checks_argv"]]
    assert any("ui_moments" in line for line in checks), "the motion gate must be wired"
    assert any("ui_lint" in line for line in checks), "the design gate must stay"


# -- the vendored starter ----------------------------------------------------------------

def test_the_starter_vendors_the_layer_the_dealer_can_deal():
    """Every moment the dealer might pick must exist in the starter, or a build mounts nothing."""
    dealer = DesignDealer(SKILLS)
    assets = STARTER / "public" / "assets"
    assert (assets / "moments" / "moments.js").is_file()
    assert (assets / "moments" / "lib.js").is_file()
    assert (assets / "moments" / "bundle.css").is_file()
    assert (assets / "motion" / "motion.css").is_file()
    for moment in dealer.moments:
        folder = assets / "moments" / moment["id"]
        assert (folder / "moment.js").is_file(), f"{moment['id']} is dealt but not vendored"
        assert (folder / "moment.css").is_file(), f"{moment['id']} has no stylesheet"


def test_the_starter_does_not_vendor_retired_moments_or_demos():
    """2 MB of the skill's demo photographs must not travel into every product."""
    assets = STARTER / "public" / "assets" / "moments"
    assert not (assets / "_demo").exists()
    assert not (assets / "gallery.html").exists()
    for retired in ("closing-lift", "headline-arrives", "lines-rise", "window-parallax",
                    "word-rotate"):
        assert not (assets / retired).exists(), f"retired moment {retired} was vendored"


def test_the_starter_links_the_layer_and_the_token_bridge_statically():
    """The three links that make a moment work, in the order they must load."""
    config = (STARTER / "nuxt.config.ts").read_text()
    for href in ("/assets/fonts/fonts.css", "/assets/design-tokens.css",
                 "/assets/motion/motion.css", "/assets/moments/bundle.css"):
        assert href in config, f"{href} must be linked statically"
    assert config.index("/assets/design-tokens.css") < config.index("/assets/moments/bundle.css")


def test_the_token_bridge_maps_every_name_the_moment_css_reads():
    """A single missed name is a silently unstyled moment, so the whole set is checked."""
    import re
    bridge = (STARTER / "public" / "assets" / "design-tokens.css").read_text()
    bundle = (STARTER / "public" / "assets" / "moments" / "bundle.css").read_text()
    needed = set(re.findall(r"var\((--[a-z0-9-]+)", bundle))
    mapped = set(re.findall(r"(--[a-z0-9-]+)\s*:", bridge))
    # Tokens a moment sets itself (its own geometry) are not the bridge's job.
    local = {name for name in needed if name.startswith(("--mo-", "--p-"))}
    missing = needed - mapped - local
    assert not missing, f"the moment CSS reads tokens nothing defines: {sorted(missing)}"


def test_the_plugin_loads_the_loader_unbundled():
    """A bundled `moments.js` resolves `/_nuxt/<id>/moment.js` and 404s every moment."""
    plugin = (STARTER / "app" / "plugins" / "moments.client.ts").read_text()
    assert "@vite-ignore" in plugin, "the import must opt out of Vite's bundling"
    assert "/assets/moments/moments.js" in plugin


# -- the engine deals it, and only to UI tasks -------------------------------------------

def test_the_engine_deals_design_into_a_ui_task_and_not_an_api_task(tmp_path):
    """The blocking end-to-end fact: the dealt block reaches the worker's instructions.

    A unit test on the dealer proves the dealer works. It does not prove a worker ever sees
    the result, and that is the failure this whole change exists to remove - the Toko Roti
    worker was handed a skill and left to choose everything itself. So this drives the real
    ``Engine`` with a scripted worker and reads the instruction the worker actually received.
    """
    from factory.config import Settings
    from factory.db import Store
    from factory.engine import Engine
    from factory.models import Plan, Profile, ProjectInput, Review

    image = "x@sha256:" + "a" * 64
    settings = Settings(data=tmp_path, operator_token="o" * 40, release_token="r" * 40,
                        model="test/model", pi="/usr/bin/true",
                        profiles={"static": Profile(image=image,
                                                    verify_argv=["python3", "/checks/static.py"],
                                                    checks_dir=str(ROOT / "checks"))})
    settings.validate()
    store = Store(tmp_path / "factory.sqlite3")
    plan = Plan(
        summary="A bakery site with a page and a health endpoint.",
        assumptions=[], questions=[],
        tasks=[
            {"id": "home-page", "title": "Build the home page",
             "instructions": "Build the landing page for the bakery.",
             "acceptance": ["The page renders the bakery name"], "depends_on": [],
             "skills": ["design-taste-frontend"]},
            {"id": "health", "title": "Add a health endpoint",
             "instructions": "Add GET /api/health returning ok.",
             "acceptance": ["The endpoint returns 200"], "depends_on": []},
        ])
    project = store.create_project(ProjectInput(name="Bakery", brief="Toko roti rumahan di Bandung"))
    with store.tx() as db:
        db.execute("UPDATE projects SET plan=?,revision=1,state='awaiting_plan' WHERE id=?",
                   (plan.model_dump_json(), project["id"]))
    store.enqueue(project["id"], "build", {"revision": 1}, allowed={"awaiting_plan"},
                  next_state="building")

    class Agent:
        def generate(self, role, instruction, context, schema, cancelled):
            return Review(approved=True, findings=[])

    class Worker:
        def __init__(self):
            self.instructions = []

        def run_task(self, worktree, instruction, model="", timeout=0,
                     cancelled=lambda: False, skills=()):
            self.instructions.append((worktree.name, instruction))
            (worktree / "index.html").write_text(
                "<!doctype html><html lang='id'><head><title>Roti</title></head>"
                "<body><h1>Roti Bu Sari</h1></body></html>")
            return "scripted"

    class Verifier:
        def verify(self, profile, source, output, job, cancelled):
            output.mkdir()
            (output / "index.html").write_text((source / "index.html").read_text())
            return "TEST DOUBLE"

    worker = Worker()
    Engine(settings, store, Agent(), Verifier(), worker=worker).process(store.claim())

    ui = [text for name, text in worker.instructions if "--p-bg:" in text]
    api = [text for name, text in worker.instructions if "--p-bg:" not in text]
    assert len(ui) == 1, "the design-declaring task must receive the dealt design"
    assert len(api) == 1, "an API-only task must not pay for a design prompt"
    block = ui[0]
    assert "--p-bg:" in block and "data-moment=" in block
    assert "do not write your own scroll" in block.lower()
    # The deal is recorded as evidence an operator can read back.
    with store.tx() as db:
        kinds = [row[0] for row in
                 db.execute("SELECT kind FROM events WHERE project_id=?", (project["id"],))]
    assert "design.dealt" in kinds
