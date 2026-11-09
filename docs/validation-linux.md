# Linux validation — native runner

Historical snapshot of native-runner validation (2026-11-07), updated 2026-11-08 with current results. Studio editing and the independent tester are documented in [Studio and tester](studio-and-tester.md); the execution model is in [architecture](architecture.md).

## Changes

Added an opt-in native Linux verifier alongside the existing Docker verifier. Administrator-owned profiles define an ordered list of argument arrays. Commands run without shell interpolation in a disposable copy of source. The verifier uses a shared wall-clock deadline, bounded captured output, existing process-group cancellation, a minimal environment without inherited model/operator credentials, and validates exported files before copying them into job output. Earlier successful source and artifacts remain available for revisions.

Operator-owned checks (`checks_dir` / `checks_argv`) run after the build in the same disposable tree. This closed a real gap: `checks_dir` had only ever been read by the Docker verifier, so the active native profile ran **no** operator checks while appearing to have them. A relative `checks_dir` resolves against the checkout containing `factory/`, not the caller's working directory.

**Moving or renaming the checkout invalidates `.venv`.** Console scripts such as `.venv/bin/pytest`, `uvicorn`, `factory` and `playwright` embed an absolute interpreter path in their shebang, and the `activate` scripts set `VIRTUAL_ENV` to the old path. After a rename they fail with `exec: .../python: not found` while `.venv/bin/python` itself still works, which makes the breakage easy to misread as an application failure. Repair with `uv sync --frozen --reinstall` and `uv venv --allow-existing .venv`, then confirm `source .venv/bin/activate` yields the new path. A rename also breaks the absolute paths in `data/secrets.env` (`FACTORY_DATA`, `FACTORY_CONFIG`), the active `data/config.json`, and any `gitdir` pointer in existing validation `products/` worktrees; an un-updated `FACTORY_DATA` silently creates a fresh empty data directory and database at the old path.

Native execution is NOT a security sandbox: programs can access files accessible to the OS account and use the network. Docker memory/CPU controls do not apply to native profiles. Pi harness workers additionally get an isolated temporary `HOME` and lose factory tokens, but provider credentials still reach them because the model call requires them. Use a dedicated unprivileged account for trusted local work. No Windows compatibility claim is made.

## Reproduce

Install Python 3.12+, uv, Git, Node.js and npm. Install the Pi 1.5.0 CLI separately and configure the exact provider/model plus its credential.

```sh
uv sync --frozen
uv run playwright install chromium
uv run factory init --data ./data
set -a
. ./data/secrets.env
set +a
export FACTORY_CONFIG="$PWD/examples/config-native-linux.json"
export FACTORY_ALLOW_NATIVE=1
export FACTORY_PI=/absolute/path/to/pi
export FACTORY_MODEL=provider/model
uv run factory api
# In another terminal with the same environment:
uv run factory scheduler --lane build
```

Create a project with profile `node`. Its generated application must include package-lock.json, a `test:integration` script, a `build` script, and export `dist/`. Adapt the administrator-owned profile for another output directory or commands. Commands are not chosen by model output. `npm ci` and lifecycle scripts execute locally with account permissions. The example has no deployment targets; do not run the deployment lane for local build-only use.

Note that the `node` example does not configure operator checks. The Studio profile does; use `examples/config-studio-linux.json` when you want the operator-check gate.

## Executed results

**2026-11-09 (current, re-measured at `3e49524`).**

- `pytest -q`: **169 passed, 3 skipped** (172 collected), 1 upstream Starlette/httpx deprecation warning. Two skips are Docker integration tests; the third is the opt-in live worker test, which passed when run separately with `FACTORY_LIVE_WORKER=1`.
- `node --test tests/workers.test.cjs`: **15 passed**.
- `ruff check factory tests scripts/validate_studio_journey.py`: passed.
- `node --check` on both dashboard scripts: passed.
- Dashboard smoke test in real Chromium: login, workspace, project detail, no page errors.

**Live model runs, `opencode-go/deepseek-v4.1-flash`:**

- Two independent plan tasks ran as concurrent Pi harness workers, each in its own worktree, and both merged into the product repository in **6.8s**, producing real `index.html` and `about.html`.
- A full pipeline passed end to end: Pi worker → build → operator secret scan → API case → browser case → artifact published (`state: ready`).
- Two workers editing the same file produced a real `merge.conflict`; the fixer worker resolved it on the target in **3 of 3 runs**, leaving the target clean. One earlier attempt failed on a 240s agent timeout, not on logic.

**2026-11-07 (historical).** 46 Python tests passed at the time of the initial native-runner work, then 58 after the Studio/tester integration; 5 then 8 JavaScript worker tests passed. Those numbers describe the code as it stood then and have been superseded.

## What the tests actually exercise

The integration tests run real components together: authenticated FastAPI requests, SQLite state transitions, the workflow engine, plan DAG scheduling, real git worktrees and merges, native subprocess execution, a real Node HTTP server answering a client request, artifact packaging, and rebuilding from previously verified source. Model responses are scripted test doubles, clearly separated from the real git, HTTP and subprocess work. The live runs listed above are separate and dated.

Negative cases cover: a failing integration test preventing publication, a build that embeds a credential being blocked by the operator secret scan, a rejected review never reaching the target branch, a worker that always fails stopping after bounded attempts, a merge conflict aborting cleanly, and native execution without explicit opt-in being refused.

Other covered behaviour: atomic claims, stale approval, cancellation, restart reconciliation, bounded repair and call budgets, path/archive rejection, timeout/output limits, API authentication, backup, and simulated release rollback.

## Editable after completion

Yes, through the plan revision workflow: edit and save the completed project's plan, approve its new revision, and rebuild. A fresh job starts from the product repository's current branch, so it inherits previously committed work. Old approvals are rejected and prior artifacts are preserved. Do not manually mutate archived job source directories, and do not hand-edit a product repository while a job is running.

Studio content editing is a separate path and is documented in [Studio and tester](studio-and-tester.md). The dashboard is NOT a full source-code editor, a visual UI editor for arbitrary components, or a chat-based change-request editor. It does display waves, per-worker outcome, branch names and review verdicts; it does not display worktree paths.

## Testing roles and limits

An automatic verifier runs configured test/build commands and operator-owned checks after independent review of each worker's diff. It can run integration suites, not just unit tests. A profile can add unit, integration, browser/E2E and build commands when their runtimes are installed. Actual coverage depends on the tests provided; exiting successfully cannot certify all business behaviour.

There is no lint or TypeScript typecheck step in the shipped profiles. The starter lockfile ships `typescript` but not `vue-tsc`, and adding a dependency requires regenerating a lockfile that has already been validated.

There is no dedicated autonomous QA agent that independently designs coverage, provisions databases, discovers all regressions, or repairs failed integration tests. A **fixer** worker exists, but only to resolve a merge conflict, not to repair a failing acceptance test. Verifier failure currently stops the workflow for revision rather than dispatching a repair agent.

Not validated: the full Nuxt Studio journey under the parallel worker lane, live Memorix roundtrip, external SAP/Microsoft integration, Windows execution, arbitrary customer applications, deployment to real servers, or production readiness.
