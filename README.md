# Anton Software Factory

A single-operator Linux software factory with SQLite workflow state, Oh My Pi model workers, independent review/testing, native or Docker verification, optional Memorix project memory, and local Nuxt Studio editing. The current milestone ends at a tested build; deployment is deferred.

Continuing agents: start with [AGENTS.md](AGENTS.md), then the [knowledge and project history](docs/knowledge/README.md).

**Release status: pre-production 0.1.0.** The workflow and container isolation have automated tests. Live model generation, Memorix interoperability, and deployment to your actual servers still require acceptance testing with your credentials. This is not yet a certified production system for POS, financial transactions, ERP, or mutually untrusted tenants.

## Visual editing and parallel tester

The dashboard now integrates local Nuxt Studio drafts and an independent tester that prepares API/browser scenarios concurrently with implementation. See [Studio setup, journey and limits](docs/studio-and-tester.md). Editing is scoped to schema-backed content; it is not arbitrary Vue source editing.

## Native Linux (without Docker)

Native Linux test/build execution is now available with explicit `FACTORY_ALLOW_NATIVE=1` opt-in and `examples/config-native-linux.json`. See [Linux validation and setup](docs/validation-linux.md) for actual results, revision support and limitations. Native execution is not a sandbox.

## Run with Docker

Requires Python 3.12+, uv, Git, Docker, the Pi 1.5.0 CLI (`npm i -g @earendil-works/pi-coding-agent`), and a model provider credential. Keep the factory on a dedicated Linux VM. Docker access grants substantial host authority; never expose its socket to generated applications.

```sh
uv sync --frozen
uv run factory init --data ./data
set -a
. ./data/secrets.env
set +a
export FACTORY_OMP=/absolute/path/to/pi
export FACTORY_MODEL=provider/model
# Configure the provider's API key in your secret manager/environment.
cp examples/config.json data/config.json
# Edit checks_dir to this checkout's absolute checks directory.
docker pull python@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df
uv run factory doctor
uv run factory api
```

In separate terminals with the same environment, run `uv run factory scheduler --lane build` and `uv run factory scheduler --lane deploy`. Open http://127.0.0.1:8080 and use the operator token from your local secret file. Never commit or paste these secrets into issues. Production approval uses a distinct release token.

Create a project, generate or enter its plan, resolve questions, and approve the exact revision. Implementer and reviewer use separate OMP invocations. Successful review is followed by operator-owned checks inside a network-disabled container. Download the verified artifact and source from the dashboard. Configure explicit per-project staging and production targets to enable release buttons.

The included static profile supports HTML/CSS/JavaScript sites; the Nuxt Studio profile adds a schema-backed editable starter. Additional application types require administrator-owned runtime images and acceptance checks; the factory does not currently ship complete CRUD, POS or ERP templates. Additional Nuxt Studio content schemas and reusable layouts remain application-specific work.

## Boundaries

- Pi is used as a tool-free structured generation client, not an unrestricted shell agent. Whole UTF-8 file changes are validated before applying. Generated code executes on the host only when native execution is explicitly enabled; Docker profiles execute it in a container.
- One build scheduler and one deployment scheduler use distinct process locks. Jobs are durable and atomically claimed. Interrupted work is marked for inspection, never blindly replayed.
- Memory is optional. `FACTORY_MEMORIX=/path/to/memorix` enables project-scoped MCP search/store. Memorix failure emits an event and does not replace SQLite task truth. The adapter targets documented `memorix_search` and `memorix_store` tools; live interoperability is not yet verified.
- Context is limited to 300 KB of text source excluding dependency lockfiles (2 MB total source limit); exported artifacts to 20 MB and 10,000 files. This release is intended for small initial projects.
- Model-call reservations default to $2 per invocation and $20 per job. `FACTORY_MAX_COST` and `FACTORY_MAX_JOB_COST` configure these limits. They are admission budgets, not guaranteed billing ceilings: a provider request already in flight can exceed an OMP cost threshold.
- The same reviewed artifact digest must pass staging before production. Lost SSH outcomes become `unknown` and block retries until reconciled.
- No automatic database migration or database rollback is performed on generated applications. Applications with state need explicit migration, backup, restore, and compatibility procedures before release.

See [Linux operations](docs/operations.md) and [architecture](docs/architecture.md).

## Validate

```sh
uv run ruff check factory tests
uv run pytest -q
FACTORY_TEST_DOCKER=1 uv run pytest tests/test_container.py -q
node --check factory/static/app.js
```

The scripted agent in tests is explicitly a test double. Passing those tests is not evidence of live AI generation. CI runs unit/API tests and real Docker integration tests. Dependencies are locked in `uv.lock`.
