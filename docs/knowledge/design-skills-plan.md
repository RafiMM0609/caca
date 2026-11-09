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

## Phase D1 — Plumbing (small)

1. `Task.skills: list[str]` — names of vendored skills for that task, default empty, capped at
   4, each matching `^[a-z][a-z0-9-]{0,40}$`. Validated against the vendor directory so a typo
   fails the plan instead of silently doing nothing.
2. `PiWorker.run_task(..., skills=())` appends `--skill <abs-path>` for each, after the existing
   `--no-skills`. Paths are resolved by the controller and must stay inside `skills/`.
3. `ParallelBuilder` passes `task.skills` through. The planner prompt mentions the field and
   that it is only for interface work.
4. **Done when:** a task declaring a skill receives it, a task that declares none does not, and
   a path that escapes `skills/` is rejected.

## Phase D2 — The skills themselves (small, mostly procurement)

5. Vendor a small, deliberate set rather than everything upstream:
   `emil-design-eng`, `animate`, `apple-design`, `mobile-native`, `break-ui`.
   Record the source commit and licence in `skills/VENDORED.md`.
6. A short `skills/README.md` explaining what each one is for, so the planner picks correctly.
7. **Done when:** the files are present, attributed, and `git status` is clean after a build.

## Phase D3 — Review (medium)

8. A design section in the reviewer prompt, written as checks rather than taste: motion
   without a stated purpose, `ease-in` on an entrance, `transition: all`, focus outline removed
   without a replacement, colour as the only state signal, fixed widths that break on a phone.
   Inline, because the reviewer has no tools.
9. **Done when:** a deliberately sloppy UI is rejected with design findings, and a clean one is
   not.

## Phase D4 — Enforcement (medium, the part that lasts)

10. `checks/ui_lint.py`: static scan of the built artifact for the mechanical subset of the
    same rules — `transition: all`, `ease-in` on entrances, `outline: none` without
    `:focus-visible`, absolute font sizes in `px` for body copy, missing `lang` attribute.
    Deliberately narrow: it catches what a regex can catch and claims nothing more.
11. Wire into the Studio profile's `checks_argv` next to `secret_scan`.
12. **Done when:** an artifact with `transition: all` fails the build, and the message names
    the file and the rule.

## Phase D5 — Proof (medium)

13. Rebuild the Perpustakaan Mini brief with design skills on the UI task, and compare against
    the recorded screenshot and artifact. Store both, because the claim being tested is
    "better", which needs a before and an after.
14. Record what improved and what did not. A skill that changes nothing is worth knowing about.

## What this plan refuses to claim

- **Not a guarantee of taste.** A skill raises the floor; it does not make a design good, and
  no check here measures whether a layout is pleasant.
- **Not measurable yet.** D4 enforces mechanical rules only. "Looks generic" stays a human
  judgement until someone defines a criterion that can be computed.
- **Not free.** Every skill on a task is prompt tokens on every worker run for that task.
