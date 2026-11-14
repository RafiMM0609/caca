# Vendored design skills

These are **not** written for this project. They are copied verbatim from third-party
repositories so a build cannot change because one of those repositories moved. Do not edit them
here: an edit makes an upgrade impossible to reason about, and these files are judgement, not code.

| | |
| --- | --- |
| Source | https://github.com/emilkowalski/skills |
| Commit | `715c1807e08c497cfd1dc4feb450a2a3300ef2a9` |
| Licence | MIT |
| Vendored | 2026-11-09 |

## Why these five

| Skill | For | Why it is here |
| --- | --- | --- |
| `emil-design-eng` | any interface work | The core skill: spacing, hierarchy, the details that make a screen look deliberate |
| `animate` | adding motion | Chooses curve, duration and properties in the order that decides if motion feels right |
| `apple-design` | gesture and spring work | Physical motion, interruptible transitions, restraint |
| `mobile-native` | phone layouts | The CSS and meta fixes between "a website" and "an app" |
| `break-ui` | stress-testing a screen | Worst-case data: long names, empty lists, huge numbers |

Deliberately excluded: the React Native, Swift and library-specific skills, and the review
skills — review runs on a tool-free client that cannot read a skill file, so it gets its rules
inline instead.

## The second source: `design-taste-frontend` (tasteskill)

| | |
| --- | --- |
| Source | https://github.com/Leonxlnx/taste-skill |
| Commit | `f6b76575f949707b4e3c37cd86cb44b0b98643e8` |
| Upstream version | tasteskill 2.5.1 |
| Licence | MIT, © Leonxlnx (`LICENSE.taste-skill`) |
| Vendored | 2026-11-11 |

| Skill | For | Why it is here |
| --- | --- | --- |
| `design-taste-frontend` | marketing, company and product pages | The anti-slop skill: one system, one section per screen, real photographs, plain copy, and a browser finish gate. Aimed at the "looks templated / AI-made" failure the five above only partly address |

Notes for anyone upgrading or debugging this one:

- **The directory name is not the upstream folder name.** Upstream keeps it in `skills/taste-skill`,
  but its `SKILL.md` declares `name: design-taste-frontend`. Pi keys a skill by that declared
  name and `tests/test_skills.py` requires directory and declared name to match, so the directory
  here is `design-taste-frontend`. `Task.skills` must spell it that way.
- **It is much larger than the others: ~40 MB, 1631 files,** because it ships `assets/`
  (photographs, icons, UI fragments, scenes, motion), `references/` (28 markdown files) and
  `scripts/` (the checker and finish gate). The other five are one `SKILL.md` each. That cost is
  paid once per repository, not per build, but it is why this skill is worth declaring only on
  tasks that build or restyle a page.
- **Its scripts need a Chrome/Chromium binary; ffmpeg is optional.** `scripts/gate.mjs`,
  `consistency.mjs`, `perf.mjs` and `assets/layout/check.mjs` all look for `CHROME_PATH` or a
  standard install location. On this host `/usr/bin/chromium-browser` exists but is **not** in
  their search list, so a worker that runs the gate must be told
  `CHROME_PATH=/usr/bin/chromium-browser`. Nothing in the factory sets that automatically, and
  the gate is not wired into `checks_argv`.
- **Its `hooks:` frontmatter is inert here.** That block is a Claude Code `Stop` hook. Pi ignores
  it, so the finish gate never runs on its own — a worker has to invoke it, or nothing enforces it.
- **All 1631 files are copied, not just the `SKILL.md`.** The skill is useless without `assets/`
  and `references/`, which it tells the model to read and copy from.

### What the factory vendored *out of* this skill

`design-taste-frontend` ships far more than a prompt: a reviewed motion layer, 13 dealt
moments, 49 font families and a checker. Three parts of it are now used at runtime, and two are
deliberately not:

| Part | Where | Why |
| --- | --- | --- |
| `assets/moments/<id>/` + `moments.js`, `lib.js` | `factory/starters/nuxt-studio/public/assets/moments/` | The page's dealt moment. Copied as static files, never bundled (a bundled loader 404s every moment). |
| `assets/motion/motion.css`, `motion.js` | `.../public/assets/motion/` | The hero entrance and the hover/press layer. |
| `library/type.json` families | `.../public/assets/fonts/` | Self-hosted `latin` woff2, so a page does not need the network at render time. |
| `library/palettes.json`, `type.json`, `assets/moments/index.json` | read in place by `factory/design.py` | Dealt on the controller. Not copied: a second copy would drift from the pin above. |
| `scripts/consistency.mjs`, `gate.mjs`, `assets/` (the rest) | **not used** | The checker needs headless Chrome (106-445 s per run on this host) and reports 53 findings on an artifact that publishes today, so it is a design-review tool, not a build gate. `checks/ui_moments.py` covers the four defects that were measured to matter, in 0.77 s. |

Run `python3 scripts/vendor_design_assets.py` after changing the commit below; it rewrites only
the three directories it owns and leaves anything else beside them alone.

## How a worker receives one

`Task.skills` names a directory here. `PiWorker` resolves it inside this directory and passes
`--skill <abs path>` alongside `--no-skills`. Discovery stays off, which is what prevents a
worker reading configuration out of the product repository; an explicitly named path still
loads. This was measured against Pi 1.5.0 rather than taken from documentation — see
[docs/knowledge/design-skills-plan.md](../docs/knowledge/design-skills-plan.md).

**Measured again for `design-taste-frontend` on Pi 1.5.2 (2026-11-11), because a 40 MB skill with
`hooks:` frontmatter is a different shape from a single markdown file:**

| Probe | Result |
| --- | --- |
| Pi's skill loader on the vendored directory | loads as `design-taste-frontend`, description 904 chars, no diagnostics |
| The exact worker argv (`--no-skills` + `--skill <path>`), tools off | skill advertised in `<available_skills>` with name, description and location; contents **not** auto-injected |
| The same argv with tools on, asked to follow the skill | worker read `SKILL.md` and quoted line 32 verbatim — so the file is reachable and followed |

The middle row is the mechanism, not a bug: a skill is a file the model reads, so a tool-free
client can never use one. That is why skills stay confined to `PiWorker`.

## Upgrading

Copy the new `SKILL.md` files, update the commit above, and rebuild one project to compare.
A skill is prompt text, so an upgrade can change output quality without touching any code this
repository owns; the commit pin is the only record of what the model was told. For
`design-taste-frontend`, copy the whole upstream `skills/taste-skill/` directory and re-run the
loader probe above, since a new upstream version may add directories the skill reads.

## Licence

MIT, © Emil Kowalski, and MIT, © Leonxlnx. Both licence texts are reproduced in
`LICENSE.upstream` and `LICENSE.taste-skill`.
