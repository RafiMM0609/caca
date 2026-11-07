# Agent guide: Anton Software Factory

Read this before changing the repository. User instructions in the current conversation take precedence.

## What this repository is for

This is an AI software factory: an operator describes an application, reviews a planner's versioned task plan, watches Oh My Pi implement/review work, and inspects tested build artifacts. A separate tester prepares API/browser cases concurrently with implementation. Supported Nuxt Content projects can be edited visually in an embedded local Nuxt Studio draft and then verified again.

The owner is starting a software company from zero. The eventual range includes portfolios, CRUD/admin apps, POS and ERP integrations. Those are long-term goals, not a claim that this release already supports all of them. The current priority is a dependable local brief-to-tested-build workflow. Deployment is deferred. Understand the reason for these choices before introducing new infrastructure.

## Read in this order

1. [Knowledge index](docs/knowledge/README.md).
2. [Origin and decisions](docs/knowledge/origin-and-decisions.md): user intent, scope changes and rationale.
3. [Handoff](docs/knowledge/handoff.md): current implementation, evidence, gaps and continuation checklist.
4. [Studio and tester](docs/studio-and-tester.md) for the current end-user journey and setup.
5. Relevant source/tests. Documentation is context; inspect code before claiming a capability exists.

## Current boundaries

- Validated execution target: Linux, Python 3.12+, Node 24, SQLite. Native execution requires `FACTORY_ALLOW_NATIVE=1`. Docker remains an optional verification profile.
- Windows was explicitly requested for local laptop use but is unfinished. A later request prioritized Linux compatibility. Preserve the outstanding Windows goal; do not claim it works or silently make Docker mandatory.
- SQLite owns workflow state. Optional Memorix owns advisory project memory. PostgreSQL is not required. Do not confuse the two stores or replace them without a concrete need.
- Oh My Pi is the chosen model runtime. It is currently a tool-free structured client; do not describe it as a fully autonomous shell worker.
- The factory dashboard is FastAPI plus static HTML/CSS/JS. Generated Nuxt applications are separate. Nuxt Studio is integrated into their local preview, not a replacement for the factory backend.
- Studio import is restricted to Markdown/JSON/YAML in `content/`. Arbitrary Vue source, media import and general drag-and-drop UI editing are not implemented.
- Tester preparation is parallel with the implementer/reviewer lane; API/browser execution waits for app readiness. It does not automatically repair failing acceptance tests.
- Legacy deployment code remains, but the current dashboard hides deployment controls and the local examples have no targets. Do not activate deployment as part of ordinary build/editor work.

## Changes and integrity

- Inspect `git status` first. Preserve user work, prior verified artifacts, source snapshots, plan revisions and draft files. Never force-push or silently discard edits.
- Keep version checks and stale-approval rejection. Changes after a successful workflow must produce a new job/revision rather than mutating the verified source.
- Never mark an artifact ready before required review/build/testing evidence passes. Native execution is not a sandbox; do not claim Docker's isolation or resource controls apply to it.
- Commands come from administrator-owned profiles as argv arrays. Avoid shell interpolation. Preserve deadlines, output bounds, cancellation and descendant-process cleanup.
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

Browser tests require `uv run playwright install chromium` or `FACTORY_CHROMIUM=/path/to/chromium`. Optional Docker tests use `FACTORY_TEST_DOCKER=1`. The opt-in real Studio journey is documented in `docs/studio-and-tester.md`; it installs dependencies and builds twice. Run relevant checks, not expensive unrelated checks for documentation-only edits.

In reports distinguish scripted model responses from live model calls, real HTTP/browser execution from unit tests, and checks actually executed from skipped or historical results. Explain concrete failures and limitations. Do not label this pre-production release production-ready.
