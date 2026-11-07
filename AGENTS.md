# Agent guide: Anton Software Factory

Read this before changing the repository. User instructions in the current conversation take precedence.

## What this repository is for

This is an AI software factory. An operator describes an application; a Pi planner produces a versioned task plan as a DAG; **Pi harness workers run the plan's ready tasks in parallel**, each in its own git worktree; a reviewer inspects each diff; a merge barrier lands accepted work into the product repository; and an independent tester plus operator-owned checks decide whether an artifact may be published. Supported Nuxt Content projects can also be edited visually in an embedded local Nuxt Studio draft and verified again.

The owner is starting a software company from zero. The eventual range includes portfolios, CRUD/admin apps, POS and ERP integrations, and products for **different clients**. Those are long-term goals, not a claim this release supports all of them. The current priority is a dependable local brief-to-tested-build workflow. Deployment is deferred. Understand the reason for these choices before introducing new infrastructure.

## Read in this order

1. [Knowledge index](docs/knowledge/README.md).
2. [Origin and decisions](docs/knowledge/origin-and-decisions.md): user intent, scope changes and rationale.
3. [Handoff](docs/knowledge/handoff.md): current implementation, evidence, gaps and continuation checklist.
4. [Architecture](docs/architecture.md): components, the worker/merge model and trust boundaries.
5. [Studio and tester](docs/studio-and-tester.md) for the editor journey and test contract.
6. Relevant source/tests. Documentation is context; inspect code before claiming a capability exists.

## The execution model you must preserve

- **The plan is a DAG** (`factory/models.py`). `ready_set()` decides what may run. Dependencies may point at any task; cycles, self-dependencies, unknown ids and duplicate ids are rejected. Never reintroduce "dependencies must appear earlier in the list".
- **A worker is one Pi harness invocation for one task** (`factory/agents.py::PiWorker`, driven by `factory/parallel.py`). Tools stay enabled. A worker edits its own worktree and must not touch files outside it.
- **One worktree per worker** (`factory/products.py`). Workers must never share a working tree, and must never write to the product repository directly.
- **Merging is a barrier.** A whole wave finishes before any merge. Branches merge one at a time. A conflicting merge is aborted, a fixer resolves it on a detached worktree of the target, and the wave retries. A failed resolution fails the job instead of publishing partial work.
- **Review reads the diff,** bounded to 200 KB. A reviewer must not be asked to trust a worker's own description of its work.
- **`PiAgent` is separate and stays tool-free.** Planning, test preparation and review use the structured JSON client. Do not use it for implementation: the runtime injects a coding-agent preamble, so a tool-free model emits markup resembling a tool call.

## Current boundaries

- Validated execution target: Linux, Python 3.12+, Node 24, SQLite. Native execution requires `FACTORY_ALLOW_NATIVE=1`. Docker remains an optional verification profile.
- Windows was explicitly requested for local laptop use but is unfinished. Preserve the outstanding Windows goal; do not claim it works or silently make Docker mandatory.
- SQLite owns workflow state, including the `workers` table. Product **code** lives in per-product git repositories under `data/products/<id>`, not in SQLite. Optional Memorix owns advisory project memory. Do not confuse these stores.
- Oh My Pi was the original runtime choice. The maintained interface is the public **Pi 1.5.0 CLI** (`@earendil-works/pi-coding-agent`). It now has two roles: a real coding harness for tasks, and a tool-free structured client for the other stages. Credentials resolve through provider environment variables, including `OPENCODE_API_KEY` for the OpenCode Go subscription.
- Products are separated by repository and directory, not by login. There is one operator and no tenant authorization; clients do not log in. Do not describe this release as multi-tenant.
- The factory dashboard is FastAPI plus static HTML/CSS/JS. Generated applications are separate. Nuxt Studio is integrated into their local preview, not a replacement for the factory backend.
- Studio import is restricted to Markdown/JSON/YAML in `content/`. An applied draft imports onto its own product branch and merges only after verification passes.
- Tester preparation runs concurrently with the worker lane; API/browser execution waits for app readiness. It does not automatically repair failing acceptance tests.
- Legacy deployment code remains, but the dashboard hides deployment controls and the local examples have no targets. Do not activate deployment as part of ordinary build/editor work.

## Changes and integrity

- Inspect `git status` first. Preserve user work, prior verified artifacts, source snapshots, plan revisions and draft files. Never force-push or silently discard edits.
- Keep version checks and stale-approval rejection. Changes after a successful workflow must produce a new job/revision rather than mutating the verified source.
- Never mark an artifact ready before required review/build/testing evidence passes. Native execution is not a sandbox; do not claim Docker's isolation or resource controls apply to it.
- Commands come from administrator-owned profiles as argv arrays. Avoid shell interpolation. Preserve deadlines, output bounds, cancellation and descendant-process cleanup.
- Worktree cleanup must remove a stale worktree still attached to a branch before that branch is deleted or reused; git refuses to recreate a worktree for an existing branch.
- Keep model budgets reserved transactionally, including concurrent tester calls. Memory failures must remain visible and must not become workflow truth.
- Do not commit tokens, `.env`/secret files, SQLite databases, `data/`, dependencies, local app state or private screenshots. Credentials belong in environment/secret configuration. Public publication to `RafiMM0609/caca` was explicitly authorized; this does not authorize disclosure of secrets or customer data.
- Record material decisions and changed capability/validation status in the handoff. Do not turn speculative plans into documented completed features.

## Validate the affected behavior

```sh
uv sync --frozen
uv run ruff check factory tests scripts/validate_studio_journey.py
uv run pytest -q
node --check factory/static/app.js
node --check factory/static/workers.js
node --test tests/workers.test.cjs
```

Optional and opt-in:

```sh
FACTORY_TEST_DOCKER=1 uv run pytest tests/test_container.py -q        # Docker profile
FACTORY_LIVE_WORKER=1 uv run pytest tests/test_worker.py -q           # real model worker
FACTORY_ALLOW_NATIVE=1 uv run python scripts/validate_studio_journey.py  # full Nuxt journey
```

Browser tests require `uv run playwright install chromium` or `FACTORY_CHROMIUM=/path/to/chromium`. The real Studio journey installs dependencies and builds twice, so it is expensive; it is documented in `docs/studio-and-tester.md`. Run relevant checks, not expensive unrelated ones for documentation-only edits.

In reports distinguish scripted model responses from live model calls, real HTTP/browser execution from unit tests, and checks actually executed from skipped or historical results. Explain concrete failures and limitations. Do not label this pre-production release production-ready.
