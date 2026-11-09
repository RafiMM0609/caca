# Vendored design skills

These are **not** written for this project. They are copied verbatim from a third-party
repository so a build cannot change because that repository moved. Do not edit them here: an
edit makes an upgrade impossible to reason about, and these files are judgement, not code.

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

## How a worker receives one

`Task.skills` names a directory here. `PiWorker` resolves it inside this directory and passes
`--skill <abs path>` alongside `--no-skills`. Discovery stays off, which is what prevents a
worker reading configuration out of the product repository; an explicitly named path still
loads. This was measured against Pi 1.5.0 rather than taken from documentation — see
[docs/knowledge/design-skills-plan.md](../docs/knowledge/design-skills-plan.md).

## Upgrading

Copy the new `SKILL.md` files, update the commit above, and rebuild one project to compare.
A skill is prompt text, so an upgrade can change output quality without touching any code this
repository owns; the commit pin is the only record of what the model was told.

## Licence

MIT, © Emil Kowalski. The licence text is reproduced in `LICENSE.upstream`.
