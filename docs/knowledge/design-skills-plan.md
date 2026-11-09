# Design skills for worker output — plan

Written 2026-11-09 after inspecting `/emilkowalski/skills` and probing Pi 1.5.0 directly.
This is a proposal; nothing here is implemented. It is separate from
[the delivery plan](plan.md), which tracks capability phases.

## The complaint that started it

The Perpustakaan Mini build passed 9 of 9 acceptance cases and the API was correct, but the
interface looked generic: grey borders, default blue buttons, no hierarchy. Nothing in the
factory checks for that, and nothing tells the worker it matters.

## What was measured before designing

Two facts were established by running Pi 1.5.0, not by reading documentation.

1. **A skill only reaches the model when tools are enabled.** `--skill <path>` with
   `--no-tools` produced no effect at all; the same call with tools enabled followed the
   skill. A skill is a file the model *reads*, so a tool-free client cannot use one. That
   confines skills to `PiWorker`; `PiAgent` (planner, tester, review) can never use them.
2. **`--no-skills` does not block an explicit `--skill`.** Discovery is disabled while an
   explicitly named path still loads, verified from a clean working directory with a control
   run that could not see the skill. This matters: it means the worker keeps its existing
   guarantee — it cannot discover configuration from the product repository — while still
   receiving operator-vendored skills.

## Where each layer belongs

| Layer | Who | Mechanism | Why there |
| --- | --- | --- | --- |
| Implement | UI workers | operator-vendored `SKILL.md` files via explicit `--skill` | Only a tool-using worker can read a skill |
| Review | reviewer | **inline** rules in the prompt, not a skill | `PiAgent` is tool-free; a skill file would be ignored |
| Enforce | verifier | `checks/ui_lint.py` in `checks_argv` | A preference that is not checked is not a rule |

Three layers because each answers a different question: *can the worker produce good design*,
*does anyone notice when it does not*, and *does the build refuse to publish it*.

## Scope decisions

- **UI tasks only.** Skills cost prompt tokens and dilute focus. An `api-*` worker needs none.
- **Vendored, not referenced.** Skills are copied into `skills/` in this repository and pinned
  by commit in a `VENDORED.md`. A build that changes behaviour because an upstream repository
  moved is not reproducible, and `git status` must show when a skill changed.
- **Not a sandbox change.** `--no-skills` stays on; only explicit paths are added.

## Phase D1 — Plumbing (small) — **DONE 2026-11-09**

1. `Task.skills: list[str]` — names of vendored skills for that task, default empty, capped at
   4, each matching `^[a-z][a-z0-9-]{0,40}$`. Validated against the vendor directory so a typo
   fails the plan instead of silently doing nothing.
2. `PiWorker.run_task(..., skills=())` appends `--skill <abs-path>` for each, after the existing
   `--no-skills`. Paths are resolved by the controller and must stay inside `skills/`.
3. `ParallelBuilder` passes `task.skills` through. The planner prompt mentions the field and
   that it is only for interface work.
4. **Done when:** a task declaring a skill receives it, a task that declares none does not, and
   a path that escapes `skills/` is rejected.
   **Done:** `Task.skills`, `PiWorker.skill_paths()`, `Settings.skills_dir()` (honouring
   `FACTORY_SKILLS_DIR`), and `ParallelBuilder` passing the list through. A missing skill and a
   name that escapes the vendor root are both errors rather than silent no-ops, because a task
   that declares a skill and silently does not get one produces exactly the generic output the
   skill existed to prevent. `tests/test_skills.py` covers all of it.

## Phase D2 — The skills themselves (small, mostly procurement) — **DONE 2026-11-09**

5. Vendor a small, deliberate set rather than everything upstream:
   `emil-design-eng`, `animate`, `apple-design`, `mobile-native`, `break-ui`.
   Record the source commit and licence in `skills/VENDORED.md`.
6. A short `skills/README.md` explaining what each one is for, so the planner picks correctly.
7. **Done when:** the files are present, attributed, and `git status` is clean after a build.
   **Done:** five skills vendored at upstream commit `715c180`, MIT, recorded in
   `skills/VENDORED.md` with the reason each is included and what was left out.

### Measured effect, before D3/D4 exist

Same task, same model, one flag apart: "create `card.html` containing one button".

| | Output |
| --- | --- |
| without skills | 233 bytes — an unstyled `<button>Click me</button>` |
| with `emil-design-eng` | 2065 bytes — easing tokens, layered shadow instead of a border, `:focus-visible`, `prefers-reduced-motion`, `100dvh`, optical letter-spacing |

That is the whole argument for D1 in one comparison. It is also not proof of *taste*: the
baseline is what an unguided agent produces, and the skilled output is what a good agent
produces. Both are correct code; only one looks deliberate.

## Phase D3 — Review (medium) — **DONE 2026-11-09**

8. A design section in the reviewer prompt, written as checks rather than taste: motion
   without a stated purpose, `ease-in` on an entrance, `transition: all`, focus outline removed
   without a replacement, colour as the only state signal, fixed widths that break on a phone.
   Inline, because the reviewer has no tools.
9. **Done when:** a deliberately sloppy UI is rejected with design findings, and a clean one is
   not.
   **Done:** the reviewer prompt gained a design section written as defects, not preferences —
   motion without purpose, `transition: all`, `ease-in` on an entrance, `outline: none` without
   a `:focus-visible` replacement, colour as the only state signal, fixed widths that break at
   360px, `user-select: none` on readable text. It ends with the counter-rule that stops it
   becoming an opinion: *do not reject one merely because a layout differs from how you would
   have drawn it*. Inline, because the reviewer has no tools and cannot read a skill file.

## Phase D4 — Enforcement (medium, the part that lasts) — **DONE 2026-11-09**

10. `checks/ui_lint.py`: static scan of the built artifact for the mechanical subset of the
    same rules — `transition: all`, `ease-in` on entrances, `outline: none` without
    `:focus-visible`, absolute font sizes in `px` for body copy, missing `lang` attribute.
    Deliberately narrow: it catches what a regex can catch and claims nothing more.
11. Wire into the Studio profile's `checks_argv` next to `secret_scan`.
12. **Done when:** an artifact with `transition: all` fails the build, and the message names
    the file and the rule.
    **Done:** `checks/ui_lint.py`, six rules, wired into the Studio profile beside
    `secret_scan`. It reports `file:line: rule` and exits 1 on any finding; an artifact with
    nothing to scan also exits 1, because a gate that sees nothing must not report success.

### What the gate found on the artifact that motivated all this

Run against the Perpustakaan Mini artifact — the one whose design prompted this work — it
reports **PASS**, with no mechanical findings. Its CSS has no `transition: all`, no `ease-in`,
no removed focus ring.

That is the honest limit of D4, and it was predicted: the complaint was *taste* — generic
colours, weak hierarchy, an unstyled form — and none of those are regex-detectable. D4 raises
the floor on the mistakes that are mechanical. D3, the reviewer, is the layer that can notice
the rest, and D5 (rebuild with skills) is the only thing that can show whether it improved.



13. Rebuild the Perpustakaan Mini brief with design skills on the UI task, and compare against
    the recorded screenshot and artifact. Store both, because the claim being tested is
    "better", which needs a before and an after.
14. Record what improved and what did not. A skill that changes nothing is worth knowing about.

## Phase D5 — Proof (medium) — **DONE 2026-11-09**

13. Rebuilt the brief with design skills on the UI tasks and compared against the earlier
    artifact. **Done**, with a deliberately harder brief: "enterprise edition" — a design
    token system, six required states (loading skeleton, empty, error, toast, inline
    validation, submit-disabled), two routed pages, modal dialogs, a confirmation dialog,
    search and filters, and mobile card-stacking at 360px.
14. What improved and what did not, recorded below.

### The build

| | |
| --- | --- |
| Plan | 5 tasks, 4 waves, **all merged attempt 1** |
| Worker time | 1578.8s of a 10800s budget (14.6%) |
| Tests | **6 of 6 passed** first attempt — 3 API, 3 browser |
| Artifact | 30 files, 244 KB |

The planner assigned skills by task without being told to: `foundation` and both UI tasks got
`emil-design-eng`, the two API tasks got **none**. That is D1's per-task design working as
intended on a real plan.

### Measured, not asserted

Screenshots are in [evidence/](evidence/). The design gate was run on both artifacts.

| | v1 (Proyek awal) | v2 (Enterprise) |
| --- | --- | --- |
| CSS custom properties | **0** | **28** |
| Ad-hoc hex colours | 31 | 13 |
| `ui_lint` findings | PASS | PASS |
| States built | form + list | loading, empty, error, toast, inline validation, submit-disabled |
| Routing | one page | `/` and `/buku`, active nav state |
| Dialogs | none | modal form + delete confirmation |
| Mobile 360px | unstyled wrap | table becomes stacked cards, toolbar vertical |

**The honest reading.** v2 is a different class of product: token-driven, routed, with the
states a real application needs, and every one of them verified by a browser test. But:

- **`ui_lint` passes both.** The gate cannot tell them apart, exactly as predicted. It catches
  mechanical defects, and neither artifact has any.
- **Most of the improvement is the brief, not the skill.** The v2 prompt specified tokens,
  states, routing and dialogs. A skill cannot invent requirements the operator never stated.
  The skill's contribution is visible at the edges — the layered shadows, the negative
  letter-spacing on headings, the explicit easing curve — and those are real but secondary.
- **One confound remains.** The brief and the skills changed together, so this run cannot
  separate "better brief" from "better skill". Separating them needs a run of the enterprise
  brief with skills disabled, which has not been done.

That last point is the one to fix before claiming the skills are what made the difference.

## What this plan refuses to claim

- **Not a guarantee of taste.** A skill raises the floor; it does not make a design good, and
  no check here measures whether a layout is pleasant.
- **Not measurable yet.** D4 enforces mechanical rules only. "Looks generic" stays a human
  judgement until someone defines a criterion that can be computed.
- **Not free.** Every skill on a task is prompt tokens on every worker run for that task.
