Vendored design skills for factory workers. See [VENDORED.md](VENDORED.md) for provenance,
licence and the reason each one is included. Not written for this project; do not edit.

Six skills from two upstreams, all MIT:

| Directory | Shape | Use for |
| --- | --- | --- |
| `design-taste-frontend` | ~40 MB: `SKILL.md`, `references/`, `assets/`, `scripts/` | Pages that must look designed, not templated: landing pages, company and product sites |
| `emil-design-eng` | one `SKILL.md` | The general default for any interface work |
| `animate` | one `SKILL.md` | Adding motion |
| `apple-design` | one `SKILL.md` | Gesture, spring and interruptible transitions |
| `mobile-native` | one `SKILL.md` | Phone layouts and PWA feel |
| `break-ui` | one `SKILL.md` | Stress-testing a screen with worst-case data |

A plan names a directory in `Task.skills`; `PiWorker` passes it as `--skill <abs path>` while
discovery stays off. `design-taste-frontend` is the directory name, not `taste-skill` — see
VENDORED.md for why.
