"""Deal the page's living parts: palette, type and motion moments, deterministically.

Why this module exists, in one measurement: a rebuild of one bakery landing page took 56
minutes, and 36 of those minutes were a single worker choosing photographs and re-checking
visual decisions it had already made (see `docs/knowledge/audit-toko-roti-54min.md`). The
worker was not writing code for most of that time. It was deliberating over choices that
nobody had made for it.

The vendored `design-taste-frontend` skill already answers those questions with dealer
scripts (`start.mjs`, `moments.mjs`), but those are Node programs, and the planner is a
tool-free structured client: it cannot run anything, and a worker that runs them pays a
Node process and a Chrome-free but still multi-second startup inside its own budget. So the
dealer lives here, in Python, on the controller:

  - It reads the skill's own libraries (`library/palettes.json`, `library/type.json`,
    `assets/moments/index.json`) rather than carrying a second copy of the judgement. If the
    skill is upgraded, this reads the new tables.
  - It is seeded by project and page kind, so the same project always gets the same deal and
    two different projects get different ones. A dealer that re-rolls per run would make a
    rebuild silently change the design.
  - It never invents a fact. It picks a palette and a type pairing for the trade, and the
    moments the page's kind is allowed to show.

What it deliberately does not do: write any page content. The palette and the moment ids are
choices; the words, prices and photographs stay the worker's job from the brief.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# The page kinds the skill recognises (`SKILL.md` section 1). `other` is not a kind: it is
# three of them, and the skill refuses to deal on it, so neither do we.
PAGE_KINDS = ("place", "product", "service", "software", "tech", "gaming")

# Words in a brief that name the kind, most specific first. Kept short on purpose: this is a
# fallback for when the planner did not say, not a classifier.
KIND_HINTS = (
    ("gaming", ("game", "permainan", "esports")),
    ("software", ("saas", "dashboard", "aplikasi", "application", "platform", "ai ", "ai,",
                  "developer tool", "fintech", "software", "app ")),
    ("tech", ("hardware", "device", "perangkat", "drone", "robot", "keyboard")),
    ("place", ("cafe", "kafe", "bakery", "toko roti", "restaurant", "restoran", "warung",
               "hotel", "guesthouse", "penginapan", "shop", "toko", "gym", "salon",
               "coffee", "kopi", "rumah makan", "kedai")),
    ("service", ("agency", "agensi", "clinic", "klinik", "law firm", "kantor hukum",
                 "studio", "konsultan", "consulting", "bengkel", "jasa")),
)

# Trades whose palette is meant for a *local place or trade* rather than for software. A brief
# can name both: "a POS app for a satay warung" is a software product, and dealing it the
# restaurant palette produced a warm paper page for a cashier application. When the kind is
# technical these trades are not candidates for the palette.
PLACE_TRADES = frozenset({"bakery", "cafe", "restaurant", "hotel", "retail", "clinic", "law",
                          "wellness", "wedding", "workshop"})

# Trades the palette library knows, for the same fallback reason.
TRADE_HINTS = (
    ("bakery", ("bakery", "roti", "bread", "kue", "pastry")),
    ("cafe", ("cafe", "kafe", "kopi", "coffee")),
    ("restaurant", ("restaurant", "restoran", "warung", "rumah makan", "kedai", "sate", "nasi")),
    ("hotel", ("hotel", "guesthouse", "penginapan", "villa", "resort")),
    ("retail", ("toko", "shop", "retail", "butik", "store")),
    ("clinic", ("clinic", "klinik", "dokter", "kesehatan")),
    ("law", ("law", "hukum", "notaris")),
    ("fintech", ("fintech", "bank", "pembayaran", "payment")),
    ("saas", ("saas", "platform", "dashboard")),
    ("devtool", ("developer", "devtool", "api", "sdk")),
    ("agency", ("agency", "agensi", "konsultan", "consulting")),
    ("studio", ("studio", "desain", "design")),
    ("wellness", ("wellness", "spa", "yoga", "kecantikan")),
    ("wedding", ("wedding", "pernikahan", "nikah")),
    ("workshop", ("workshop", "bengkel", "workshop", "kerajinan")),
    ("hardware", ("hardware", "perangkat", "device", "drone")),
    ("game", ("game", "permainan", "esports")),
)

# How many moments a page gets. `moments.md`: one on a short page, two on a long one (eight
# sections or more), never three. We deal one, because the worker decides the page's length
# and the skill's rule for two is about a page we have not seen yet; a second moment on a
# short page is a fault the checker reports (`moments-over`).
MOMENTS_PER_PAGE = 1


@dataclass
class Deal:
    """The dealt design for one page: what the worker must build, not how."""

    page_kind: str
    trade: str
    palette: dict = field(default_factory=dict)
    type_pairing: dict = field(default_factory=dict)
    moments: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "page_kind": self.page_kind,
            "trade": self.trade,
            "palette": self.palette,
            "type": self.type_pairing,
            "moments": self.moments,
            "notes": self.notes,
        }


def _seeded(seed: str, count: int) -> int:
    """A stable index for one seed, so the same project keeps its deal across rebuilds."""
    if count <= 0:
        return 0
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % count


def detect_kind(brief: str, declared: str = "") -> str:
    """The page kind: what the plan declared, else a hint from the brief, else a product page.

    `product` is the safest default: it is the only kind whose rules are permissive enough
    that being wrong costs a photograph rather than an invented effect (a `place` page that
    gets shader effects is a house-rule violation, a `product` page that is really a `place`
    just looks less photographic than it could).
    """
    declared = (declared or "").strip().lower()
    if declared in PAGE_KINDS:
        return declared
    text = f" {brief.lower()} "
    for kind, words in KIND_HINTS:
        if any(word in text for word in words):
            return kind
    return "product"


def detect_trade(brief: str, page_kind: str) -> str:
    """The trade the palette library files a palette under.

    A technical kind is never dealt a place's trade. The trade still decides the *subject*,
    but the palette has to answer a software page, so the hint list is skipped entirely and
    the kind's own default is used. Measured: a POS brief naming "warung sate" was dealt the
    restaurant palette (warm paper, ochre accent), which is right for a warung and wrong for
    a cashier application.
    """
    technical = page_kind in ("software", "tech", "gaming")
    text = f" {brief.lower()} "
    for trade, words in TRADE_HINTS:
        if technical and trade in PLACE_TRADES:
            continue
        if any(word in text for word in words):
            return trade
    return {"place": "restaurant", "product": "consumer-product", "service": "studio",
            "software": "saas", "tech": "hardware", "gaming": "game"}.get(page_kind, "studio")


class DesignDealer:
    """Reads the vendored skill's own tables and deals one page's design.

    Nothing here calls a model, spawns Node or opens a browser. It is a table lookup with a
    seeded choice, which is the whole point: the expensive part of design work was the
    deliberation, and deliberation over a fixed table is a lookup.
    """

    def __init__(self, skills_dir: Path, skill: str = "design-taste-frontend"):
        self.root = Path(skills_dir) / skill
        self._palettes = None
        self._types = None
        self._moments = None

    # -- the skill's tables -------------------------------------------------------------
    def available(self) -> bool:
        return (self.root / "library" / "palettes.json").is_file()

    def _load(self, relative: str):
        path = self.root / relative
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError) as exc:
            logger.warning("design dealer could not read %s: %s", relative, exc)
            return None

    @property
    def palettes(self) -> list[dict]:
        if self._palettes is None:
            self._palettes = self._load("library/palettes.json") or []
        return self._palettes

    @property
    def types(self) -> list[dict]:
        if self._types is None:
            self._types = self._load("library/type.json") or []
        return self._types

    @property
    def moments(self) -> list[dict]:
        if self._moments is None:
            payload = self._load("assets/moments/index.json") or {}
            # A retired moment has been withdrawn by the skill; dealing one would hand the
            # worker a part the skill's own checker rejects.
            self._moments = [m for m in payload.get("moments", []) if not m.get("retired")]
        return self._moments

    # -- dealing ------------------------------------------------------------------------
    def palette_for(self, trade: str, seed: str) -> dict | None:
        """The first palette made for this trade, chosen stably from the trade's pool.

        The skill says "take the first" from three printed for the trade. Three is what its
        dealer prints; here the whole pool for the trade is candidates and the seed picks one,
        so two clients in the same trade do not get the same colours.
        """
        pool = [p for p in self.palettes if trade in (p.get("trades") or [])]
        if not pool:
            return None
        return pool[_seeded(f"{seed}:palette", len(pool))]

    def type_for(self, page_kind: str, seed: str) -> dict | None:
        """A type pairing weighted toward the kinds the pairing was made for."""
        pool = [t for t in self.types if (t.get("kinds") or {}).get(page_kind)]
        if not pool:
            pool = self.types
        if not pool:
            return None
        # Weight by how strongly the pairing suits this kind: a pairing that scores 3 for
        # `place` is a better deal there than one that scores 1.
        weighted = []
        for entry in pool:
            weight = int((entry.get("kinds") or {}).get(page_kind, 1))
            weighted.extend([entry] * max(1, weight))
        return weighted[_seeded(f"{seed}:type", len(weighted))]

    def moments_for(self, page_kind: str, has_photo: bool, seed: str,
                    count: int = MOMENTS_PER_PAGE) -> list[dict]:
        """The moments this page kind is allowed, cheapest first, chosen stably.

        Filtered by the skill's own rules: a moment must list the kind in `pages`, a
        photograph-led moment needs a photograph, and WebGL stays off kinds that are not
        technical (a cafe gets photographs, never a shader).
        """
        candidates = []
        for moment in self.moments:
            if page_kind not in (moment.get("pages") or []):
                continue
            if (moment.get("needs") or {}).get("photo") and not has_photo:
                continue
            if int(moment.get("webgl") or 0) > 0 and page_kind not in ("software", "tech", "gaming"):
                continue
            candidates.append(moment)
        if not candidates:
            return []
        # Cheapest first, then stable: a `low`-cost moment is the default, and a `mid` one is
        # only reached when nothing cheaper fits. That is the resource rule, not taste.
        order = {"low": 0, "mid": 1}
        candidates.sort(key=lambda m: (order.get(m.get("cost"), 2), m.get("id", "")))
        cheapest = [m for m in candidates if order.get(m.get("cost"), 2) ==
                    order.get(candidates[0].get("cost"), 2)]
        picked = []
        start = _seeded(f"{seed}:moment", len(cheapest))
        for offset in range(min(count, len(cheapest))):
            picked.append(cheapest[(start + offset) % len(cheapest)])
        return picked

    def deal(self, brief: str, seed: str, page_kind: str = "", has_photo: bool = True) -> Deal:
        """Deal one page. Never raises: a missing skill degrades to notes, not a failed build."""
        kind = detect_kind(brief, page_kind)
        trade = detect_trade(brief, kind)
        deal = Deal(page_kind=kind, trade=trade)
        if not self.available():
            deal.notes.append(
                "The vendored design skill is missing, so no palette, type or moment was "
                "dealt. Build the page from the brief and the skill's own guidance.")
            return deal
        palette = self.palette_for(trade, seed)
        if palette:
            deal.palette = {
                "id": palette.get("id"), "name": palette.get("name"),
                "bg": palette.get("bg"), "surface": palette.get("surface"),
                "ink": palette.get("ink"), "muted": palette.get("muted"),
                "accent": palette.get("accent"), "accent_ink": palette.get("accentInk"),
                "note": palette.get("note"),
            }
        else:
            deal.notes.append(f"No palette in the library is filed under the trade {trade!r}.")
        pairing = self.type_for(kind, seed)
        if pairing:
            deal.type_pairing = {
                "id": pairing.get("id"),
                "display": (pairing.get("display") or {}).get("family"),
                "text": (pairing.get("text") or {}).get("family"),
                "mono": (pairing.get("mono") or {}).get("family"),
                "display_weight": (pairing.get("display") or {}).get("w"),
                "tracking": (pairing.get("display") or {}).get("tracking"),
                "suits": pairing.get("suits"),
            }
        moments = self.moments_for(kind, has_photo, seed)
        deal.moments = [{"id": m.get("id"), "name": m.get("name"), "kind": m.get("kind"),
                         "trigger": m.get("trigger"),
                         "look": _first_sentence(m.get("look", "")),
                         "needs": m.get("needs") or {}, "cost": m.get("cost"),
                         "webgl": m.get("webgl")} for m in moments]
        if not deal.moments:
            deal.notes.append(
                f"No moment in the library lists the page kind {kind!r}, so this page has "
                "none. Do not invent a scroll animation to fill the gap.")
        return deal


def _first_sentence(text: str, limit: int = 320) -> str:
    """The first sentence or two of a skill's description, cut at a word boundary.

    The `look` fields in the moment index are written for the skill's own dealer report, and
    carry bookkeeping a worker does not need - "the print look is not dealt: tech leaves this
    moment out, so it is kept only as opts look print" is an instruction to the *skill*, not to
    the page builder. `photo-chapters`'s description is 551 characters and `picture-develops`'s
    is 503; every one is repeated on every run of the task that carries it, so the trim is a
    prompt-cost saving on the design task, not cosmetic tidying.
    """
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    cut = text[:limit]
    for stop in (". ", "; "):
        at = cut.rfind(stop)
        if at > limit // 2:
            return cut[:at + 1].strip()
    return cut.rsplit(" ", 1)[0].rstrip(",;:") + " ..."


def deal_blocks(deal: Deal) -> str:
    """The dealt design as text a worker can act on, for `instructions_for`.

    Written as instructions, not as data: a worker given a JSON blob of hex codes will paste
    them into a new stylesheet, which is exactly the 700-line re-invention this replaces.
    """
    lines = ["DEALT DESIGN (this page's design is already decided; build it, do not re-choose it)",
             f"- Page kind: {deal.page_kind}. Trade: {deal.trade}."]
    if deal.palette:
        p = deal.palette
        lines.append(f"- Palette {p.get('id')} ({p.get('name')}), the first filed under "
                     f"{deal.trade!r}. Set these on :root and build with them; do not invent "
                     f"other colours:")
        lines.append(f"    --p-bg: {p.get('bg')}; --p-surface: {p.get('surface')}; "
                     f"--p-ink: {p.get('ink')}; --p-muted: {p.get('muted')}; "
                     f"--p-accent: {p.get('accent')}; --p-accent-ink: {p.get('accent_ink')};")
        if p.get("note"):
            lines.append(f"    Why it fits the trade: {p.get('note')}")
    if deal.type_pairing:
        t = deal.type_pairing
        mono = f", mono {t['mono']}" if t.get("mono") else ""
        lines.append(f"- Type {t.get('id')}: display {t.get('display')}, text "
                     f"{t.get('text')}{mono}. The fonts are self-hosted in "
                     f"`public/assets/fonts/` with only that family's files; use "
                     f"`--font-display` and `--font-text` and no other family.")
    if deal.moments:
        lines.append("- Motion: the page has exactly one dealt moment (the skill's house rule "
                     "is one on a short page, two on a long one, never more). Build it by "
                     "copying the markup pattern below into a section of its own; the moment's "
                     "CSS and JS are already vendored and already linked.")
        for m in deal.moments:
            lines.append(f"    <section class=\"section\" data-moment=\"{m['id']}\"> ...")
            lines.append(f"    {m['id']} ({m.get('name')}): {m.get('look')}")
            needs = m.get("needs") or {}
            if needs.get("words"):
                lines.append(f"      It needs {needs['words']}.")
            if needs.get("photo") or needs.get("pictures"):
                photos = needs.get("pictures") or "a photograph with a clear subject"
                lines.append(f"      It needs {photos}.")
        lines.append("    Do not add a second moment, and do not write your own scroll "
                     "animation, parallax, marquee or reveal: the vendored layer already "
                     "provides the hero entrance and the hover/press states, and a hand-written "
                     "one fights it.")
        # The pinned moments reserve space for a fixed header using `var(--nav)`, which the
        # token bridge defaults to the skill's own 72px. A page whose header is a different
        # height must say so, or the section's heading is computed against the wrong offset.
        # Measured: with the bridge at 0, a 73px sticky header covered the chapter heading.
        lines.append("    - If the page has a sticky or fixed header, set --p-nav-height on "
                     "`:root` to that header's exact height in px. The moment reserves space "
                     "for a 72px header by default, and a different height will either leave a "
                     "gap or hide the section's heading behind the header. A page with no "
                     "sticky header sets --p-nav-height: 0px.")
    for note in deal.notes:
        lines.append(f"- Note: {note}")
    return "\n".join(lines)
