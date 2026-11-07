# Linux validation — 2026-11-07

## Changes

Added an opt-in native Linux verifier alongside the existing Docker verifier. Administrator-owned profiles define an ordered list of argument arrays. Commands run without shell interpolation in a disposable copy of source. The verifier uses a shared wall-clock deadline, bounded captured output, existing process-group cancellation, a minimal environment without inherited model/operator credentials, and validates exported files before copying them into job output. Earlier successful source and artifacts remain available for revisions.

Native execution is NOT a security sandbox: programs can access files accessible to the OS account and use the network. Docker memory/CPU controls do not apply to native profiles. Use a dedicated unprivileged account for trusted local work. No Windows compatibility claim is made.

## Reproduce

Install Python 3.12+, uv, Git, Node.js and npm. Install OMP separately and configure the exact provider/model plus its credential for real generation.

```sh
uv sync --frozen
uv run factory init --data ./data
set -a
. ./data/secrets.env
set +a
export FACTORY_CONFIG="$PWD/examples/config-native-linux.json"
export FACTORY_ALLOW_NATIVE=1
export FACTORY_OMP=/absolute/path/to/omp
export FACTORY_MODEL=provider/model
uv run factory api
# In another terminal with the same environment:
uv run factory scheduler --lane build
```

Create a project with profile `node`. Its generated application must include package-lock.json, a `test:integration` script, a `build` script, and export `dist/`. Adapt the administrator-owned profile for another output directory or commands. Commands are not chosen by model output. `npm ci` and lifecycle scripts execute locally with account permissions. The example has no deployment targets; do not run the deployment lane for local build-only use. Legacy deployment controls/code remain present, but are not configured by this example.

## Executed results

- `pytest -q`: **46 passed, 2 skipped**, 1 upstream Starlette/httpx deprecation warning.
- `node --test tests/workers.test.cjs`: **5 passed**.
- `ruff check factory tests`: passed.
- `node --check factory/static/app.js`: passed.
- The two skipped tests are Docker integration tests, not native tests. They passed in the previous validation session but were not rerun for this change.

The new integration tests exercise actual components together: authenticated FastAPI requests, SQLite state transitions, the workflow engine, native subprocess execution, npm dependency-lock installation for a zero-external-dependency fixture, a real Node HTTP server/client request asserting its JSON contract, npm build, artifact packaging, then plan editing and rebuilding from previously verified source. Model responses are a scripted test double, clearly separated from actual Node execution. No paid/live model call was made.

The negative integration case changes the expected HTTP response so the test fails. The project becomes failed, no artifact is published, and the subsequent build command is not executed. Native execution without explicit opt-in is also rejected.

Existing tests cover atomic claims, stale approval, cancellation, restart reconciliation, bounded repair and call budgets, review failures, path/archive rejection, timeout/output limits, API authentication, backup, and simulated release rollback.

## Editable after completion

Yes, through the plan revision workflow: edit and save the completed project's plan, approve its new revision, and rebuild. A fresh job starts from the previous verified source. Old approvals are rejected and prior artifacts are preserved. Do not manually mutate archived job source directories.

The dashboard currently edits plan JSON and displays generated source/evidence. It is NOT yet a full source-code editor, visual UI editor, Nuxt Studio integration, or a chat-based change-request editor. Those capabilities must not be inferred from plan editability.

## Testing roles and limits

An automatic verifier executes configured test/build commands after independent AI code review. It can run integration suites, not just unit tests. A profile can add unit, integration, browser/E2E and build commands when their runtimes are installed. Actual coverage depends on the tests provided; exiting successfully cannot certify all business behavior.

There is no dedicated autonomous QA agent that independently designs coverage, provisions databases, discovers all regressions, or repairs failed integration tests. The planner can assign test-writing tasks to the implementer. Review is separate; verifier failure currently stops the workflow for revision rather than automatically dispatching a repair agent.

Not validated: live OMP provider generation/review, live Memorix roundtrip, external SAP/Microsoft integration, browser UI interaction, Windows execution, arbitrary customer applications, deployment to real servers, or production readiness. Memory and model credentials are not configured in this environment.
