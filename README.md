# Anton Software Factory

A single-operator Linux software factory. An operator writes a brief; a Pi planner turns it into a versioned task plan; **parallel Pi harness workers** implement each task in its own git worktree; an independent tester and operator-owned checks gate the result; and a tested artifact is published. The current milestone ends at a tested build — deployment is deferred.

Continuing agents: start with [AGENTS.md](AGENTS.md), then the [knowledge and project history](docs/knowledge/README.md).

**Release status: pre-production 0.1.0.** The workflow, parallel worker lane and container isolation have automated tests. Live end-to-end runs were performed with `opencode-go/deepseek-v4.1-flash`, including a full Nuxt Studio journey re-run under the parallel worker lane and a SQLite CRUD application that passed 11 of 11 acceptance cases. See [handoff](docs/knowledge/handoff.md) for the dated evidence and its caveats; the run logs live in ignored local directories and are not in this repository.

**The loop now closes.** On 2026-11-09 a single `build` job ran a complete brief to a published artifact in 15.3 minutes: planner, a four-task DAG, four Pi workers (two concurrent), merge barrier, build, and three acceptance cases — two of them real browser cases — all passed. The artifact was then extracted and built outside the factory, and served correctly. Phase 0 of the [delivery plan](docs/knowledge/plan.md) is closed; Phase 1 (a repair loop for failing gates) is next. This is still not a certified production system for POS, financial transactions, ERP, or mutually untrusted tenants.

## How work runs

```
brief → Pi planner → plan as a DAG → waves of Pi harness workers (one worktree each)
      → review each diff → merge barrier → build + operator checks + API/browser tester → artifact
```

- **The plan is a DAG.** Tasks declare `depends_on`, and every task whose dependencies have merged is eligible to run at once. Order comes from the graph, not from list position. Cycles, self-dependencies and unknown ids are rejected.
- **A worker is a real Pi harness.** It reads files, runs commands and edits its own worktree with tools enabled. It does not return whole files as JSON.
- **Merging is a barrier.** All workers in a wave finish, then accepted branches merge into the product branch one at a time, so no worker finishes against a base that has moved. A conflicting merge is aborted (the target is never left half-merged), a fixer worker resolves the integration, and the wave retries.
- **Review sees the diff,** not the worker's own account of its work. Build, operator checks and API/browser cases must all pass before an artifact is published.

## Products are git repositories

Each product owns an independent repository at `data/products/<project-id>`. There is no git repository for generated products inside this checkout, and `data/` is git-ignored by design. A product repository is created on first build, starts empty for a new project, or contains the copied starter when the Studio starter is used.

This is what makes per-worker worktrees, reviewable diffs, rollback and future client hand-off possible.

## Native Linux

Native execution is the validated default for local work. It requires an explicit `FACTORY_ALLOW_NATIVE=1` opt-in. See [Linux validation and setup](docs/validation-linux.md).

```sh
uv sync --frozen
uv run playwright install chromium
uv run factory init --data ./data
set -a
. ./data/secrets.env
set +a
export FACTORY_CONFIG="$PWD/examples/config-studio-linux.json"
export FACTORY_ALLOW_NATIVE=1
export FACTORY_PI=/absolute/path/to/pi
export FACTORY_MODEL=opencode-go/deepseek-v4.1-flash
uv run factory api
# In another terminal with the same environment:
uv run factory scheduler --lane build
```

Open http://127.0.0.1:8080 and use the operator token from your local secret file. Never commit or paste these secrets.

Requires Python 3.12+, uv, Git, Node 24, the Pi 1.5.0 CLI (`npm i -g @earendil-works/pi-coding-agent`), and a provider credential.

## Configuring the environment

All configuration is environment variables plus one optional JSON file. There is no config
wizard: `factory init` writes a secret file with the two tokens, and everything else is a
variable you export before starting the API and the scheduler.

```sh
factory init --data ./data     # writes data/secrets.env, mode 0600, never overwrites
```

The file `data/secrets.env` holds the two tokens. Keep it out of version control (`data/` is
git-ignored by design) and load it with `set -a; . ./data/secrets.env; set +a`. Both processes
must share the same environment, because the scheduler reserves budget the API reported.

### Required

| Variable | Meaning |
| --- | --- |
| `FACTORY_OPERATOR_TOKEN` | Operator credential, at least 32 characters. Written by `factory init`. |
| `FACTORY_RELEASE_TOKEN` | Separate production-release credential, at least 32 characters, must differ from the operator token. |
| `FACTORY_MODEL` | Exact `provider/model`, for example `opencode-go/deepseek-v4.1-flash`. Planning, test preparation and review all fail without it. |

A provider credential must also be exported: `OPENCODE_API_KEY`, `ANTHROPIC_API_KEY`,
`OPENAI_API_KEY`, `GEMINI_API_KEY`, `OPENROUTER_API_KEY`, or another key the Pi CLI reads. These
reach model workers only; generated applications never receive them.

### Useful path and behaviour switches

| Variable | Default | Meaning |
| --- | --- | --- |
| `FACTORY_DATA` | `./data` | SQLite store, job evidence, product repositories and served copies. |
| `FACTORY_CONFIG` | `<data>/config.json` | JSON file holding `profiles` and `targets`. `examples/config-studio-linux.json` is the Studio profile. |
| `FACTORY_PI` | `pi` | Path to the Pi CLI if it is not on `PATH`. |
| `FACTORY_ALLOW_NATIVE` | unset | Must be `1` to execute locally. Native execution is not a sandbox. |
| `FACTORY_SKILLS_DIR` | the shipped `skills/` | Where vendored design skills are resolved from. |
| `FACTORY_CHROMIUM` | Playwright's own | Browser binary for the tester when Playwright's download is absent. |
| `FACTORY_PLAYWRIGHT_BROWSERS` | `~/.cache/ms-playwright` | Browser directory handed to model workers, so a design task can look at its own page without installing a second browser. Exported only when the directory exists. |
| `FACTORY_NPM_CACHE` | a per-run temp dir | Shared npm cache for verification and local runs. |
| `FACTORY_MEMORIX` | disabled | Path to a Memorix binary for optional project memory. Advisory only; SQLite stays the truth. |

### Timeouts

Each stage has its own clock, because one model call and one worker run are different units of
work.

| Variable | Default | Bounds |
| --- | --- | --- |
| `FACTORY_AGENT_TIMEOUT` | 600s | One tool-free structured call: planner, tester preparation, review. |
| `FACTORY_WORKER_TIMEOUT` | 1800s | One Pi harness worker that may edit a tree and build it. |
| `FACTORY_SKILL_WORKER_TIMEOUT` | 3600s | The same, for a task that declares a design skill, because design work is a build-look-adjust loop. Must be at least `FACTORY_WORKER_TIMEOUT` and at most `FACTORY_JOB_TIMEOUT`. |
| `FACTORY_JOB_TIMEOUT` | 7200s | Floor for one whole job; scaled upward by the plan's wave count. |
| `FACTORY_MAX_JOB_TIMEOUT` | 21600s | Hard cap for that scaled estimate. |

### Cost reservations

Nothing is billed by this system. Every model call and worker run **reserves** an amount against
`jobs.reserved_usd` inside a transaction before it starts, so concurrent workers cannot
collectively pass the ceiling. Reservations are admission estimates, not measured spend: a build
that reserved $20 can have cost a fraction of a cent on the provider.

| Variable | Default | Meaning |
| --- | --- | --- |
| `FACTORY_MAX_COST` | $2.00 | Reserved per structured model call. |
| `FACTORY_WORKER_COST` | $0.25 | Reserved per harness worker; `0` stops metering workers. |
| `FACTORY_MAX_JOB_COST` | $20.00 | Ceiling for one job's reservations. |
| `FACTORY_WORKER_CONCURRENCY` | 3 | Simultaneous workers, each its own process tree. Lower it on a small host: two concurrent Nuxt builds can exhaust 2 GB. |

**Size `FACTORY_MAX_JOB_COST` from the plan, not by taste.** An N-task plan reserves roughly
`N × FACTORY_MAX_COST + FACTORY_MAX_COST + N × FACTORY_WORKER_COST`, because every task is
reviewed and every review is a structured call. At the defaults that is exactly $20 for eight
tasks, so an eight-task plan fails on its last task with `WorkerBudgetExhausted` unless the
ceiling is raised. Raise it to cover the largest plan you intend to run:

```sh
export FACTORY_MAX_JOB_COST=60   # covers roughly 25 tasks at the default reservation sizes
```

A failed job keeps the reservations it made; re-approving the same revision enqueues a new job
with a fresh ledger and resumes from the tasks already merged.

### Checking the result

```sh
uv run factory doctor    # resolved Pi path, model, profiles, and every check each profile runs
```

## The dashboard

The operator UI is FastAPI plus one static HTML/CSS/JS shell — no build step, no framework, no
`node_modules` on the host. It is deliberately plain: the expensive part of this system is
`npm ci`, `nuxt build` and Chromium, so a frontend toolchain would add weight to a 2 GB box and
buy nothing.

Two things about it are worth knowing before changing it:

- **The UI is Indonesian.** The operator reads Indonesian, and the shell had drifted into mixed
  English chrome with Indonesian notices. `label()` maps every machine state (`awaiting_plan`,
  `changes_requested`) to an Indonesian label, because printing a raw state at the operator is
  how "None" ended up meaning "no draft yet".
- **Events are live.** `GET /api/projects/<id>/stream` is a resumable server-sent event stream
  keyed on the global event `seq`, read with `fetch()` rather than `EventSource` because this API
  is bearer-only and `EventSource` cannot send an `Authorization` header. Everything else the
  dashboard needs still polls, with backoff, and stops entirely while the tab is hidden. If the
  stream cannot connect it falls back to cursor polling — slower, not broken. See
  [operations](docs/operations.md#live-updates-and-the-reverse-proxy) before putting a proxy in
  front of it.

## Visual editing and the independent tester

The dashboard integrates local Nuxt Studio drafts and an independent tester that prepares API/browser cases while workers implement. Editing is scoped to schema-backed content, not arbitrary Vue source. See [Studio setup, journey and limits](docs/studio-and-tester.md).

## Boundaries

- **Native workers are not sandboxed.** Each worker gets a temporary `HOME`, `PI_CODING_AGENT_DIR` and XDG directories, so `~/.ssh`, `~/.aws` and the operator's git identity are unreachable, and factory tokens are removed from its environment. Provider credentials still reach the worker because the model call requires them: a worker that can run `env` can observe the provider key.
- One build scheduler and one deployment scheduler use distinct process locks. Jobs are durable and atomically claimed. Interrupted work is marked for inspection, never blindly replayed.
- Memory is optional. `FACTORY_MEMORIX=/path/to/memorix` enables project-scoped MCP search/store. Memorix failure emits an event and does not replace SQLite task truth. Live interoperability is not yet verified.
- `FACTORY_WORKER_CONCURRENCY` (default 3, max 16) bounds simultaneous workers. Each runs as its own process tree and applies `FACTORY_WORKER_TIMEOUT`, or `FACTORY_SKILL_WORKER_TIMEOUT` when its task declares a design skill.
- Context passed to a **reviewer** is a bounded diff (200 KB) rather than the whole tree. The 300 KB text limit and 2 MB source limit still govern source snapshots served to the dashboard.
- Model calls and harness worker runs reserve against one job ceiling, so a wave cannot spend past `FACTORY_MAX_JOB_COST`. Reservations are admission estimates, not metered bills; see [cost reservations](#cost-reservations) for the formula and why a larger plan needs a larger ceiling.
- The same reviewed artifact digest must pass staging before production. Lost SSH outcomes become `unknown` and block retries until reconciled.
- No automatic database migration or rollback is performed on generated applications.
- Legacy deployment code remains, but the dashboard hides deployment controls and the local examples have no targets. Do not activate deployment as part of ordinary build or editor work.

See [architecture](docs/architecture.md), [Linux operations](docs/operations.md) and [handoff](docs/knowledge/handoff.md).

## Validate

```sh
uv sync --frozen
uv run ruff check factory tests scripts/validate_studio_journey.py
uv run pytest -q
node --check factory/static/app.js
node --check factory/static/workers.js
node --test tests/workers.test.cjs
```

Optional: `FACTORY_LIVE_WORKER=1 uv run pytest tests/test_worker.py -q` for a real model worker call. Browser tests need `uv run playwright install chromium` or `FACTORY_CHROMIUM=/path/to/chromium`. Add `FACTORY_UI_TEST=1` to run the dashboard browser smoke test (~390 MB for Chromium; keep it out of the build lane — see [operations](docs/operations.md#optional-browser-test)).

Model responses in the default suite are scripted test doubles. Passing those tests is not evidence of live AI generation. The live runs recorded in the handoff are dated and separate. Dependencies are locked in `uv.lock`.
