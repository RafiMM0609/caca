# Anton Software Factory

A single-operator Linux software factory. An operator writes a brief; a Pi planner turns it into a versioned task plan; **parallel Pi harness workers** implement each task in its own git worktree; an independent tester and operator-owned checks gate the result; and a tested artifact is published. The current milestone ends at a tested build — deployment is deferred.

Continuing agents: start with [AGENTS.md](AGENTS.md), then the [knowledge and project history](docs/knowledge/README.md).

**Release status: pre-production 0.1.0.** The workflow, parallel worker lane and container isolation have automated tests. Live end-to-end runs were performed with `opencode-go/deepseek-v4.1-flash`, including a full Nuxt Studio journey re-run under the parallel worker lane and a SQLite CRUD application that passed 11 of 11 acceptance cases. See [handoff](docs/knowledge/handoff.md) for the dated evidence and its caveats; the run logs live in ignored local directories and are not in this repository.

**The parts are proven; the whole is not.** No single `build` job has ever completed end to end — every published artifact came from a separately enqueued `verify` job driven by a script. Closing that loop is Phase 0 of the [delivery plan](docs/knowledge/plan.md). This is not a certified production system for POS, financial transactions, ERP, or mutually untrusted tenants.

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

## Native Linux (without Docker)

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

## Visual editing and the independent tester

The dashboard integrates local Nuxt Studio drafts and an independent tester that prepares API/browser cases while workers implement. Editing is scoped to schema-backed content, not arbitrary Vue source. See [Studio setup, journey and limits](docs/studio-and-tester.md).

## Boundaries

- **Native workers are not sandboxed.** Each worker gets a temporary `HOME`, `PI_CODING_AGENT_DIR` and XDG directories, so `~/.ssh`, `~/.aws` and the operator's git identity are unreachable, and factory tokens are removed from its environment. Provider credentials still reach the worker because the model call requires them: a worker that can run `env` can observe the provider key. Docker's CPU/RAM isolation does not apply.
- One build scheduler and one deployment scheduler use distinct process locks. Jobs are durable and atomically claimed. Interrupted work is marked for inspection, never blindly replayed.
- Memory is optional. `FACTORY_MEMORIX=/path/to/memorix` enables project-scoped MCP search/store. Memorix failure emits an event and does not replace SQLite task truth. Live interoperability is not yet verified.
- `FACTORY_WORKER_CONCURRENCY` (default 3, max 16) bounds simultaneous workers. Each runs as its own process tree and applies the timeout `FACTORY_AGENT_TIMEOUT`.
- Context passed to a **reviewer** is a bounded diff (200 KB) rather than the whole tree. The 300 KB text limit and 2 MB source limit still govern source snapshots served to the dashboard.
- Model-call reservations default to $2 per invocation and $20 per job. Only planning, review and test preparation consume the budget — harness workers are not metered per call. `FACTORY_MAX_COST` and `FACTORY_MAX_JOB_COST` configure these. They are admission budgets, not guaranteed billing ceilings.
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

Optional: `FACTORY_TEST_DOCKER=1 uv run pytest tests/test_container.py -q` for the Docker profile, and `FACTORY_LIVE_WORKER=1 uv run pytest tests/test_worker.py -q` for a real model worker call. Browser tests need `uv run playwright install chromium` or `FACTORY_CHROMIUM=/path/to/chromium`.

Model responses in the default suite are scripted test doubles. Passing those tests is not evidence of live AI generation. The live runs recorded in the handoff are dated and separate. Dependencies are locked in `uv.lock`.
