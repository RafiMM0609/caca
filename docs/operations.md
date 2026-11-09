# Linux operations

Use a dedicated VM, local disk for SQLite, HTTPS reverse proxy, firewall, and encrypted backups. Do not place SQLite on NFS. Do not expose port 8080 publicly; bind to loopback and use the supplied Caddy example. The API intentionally has no cookie auth or permissive CORS.

Install the checkout and locked virtualenv at `/opt/software-factory`. Configure `FACTORY_DATA=/var/lib/software-factory` and `FACTORY_CONFIG=/etc/software-factory/config.json`. Store tokens and provider credentials in a mode-0600 environment file referenced by systemd. Use separate deployment credentials and accounts; build workers must not receive production SSH keys. For a first single-user setup the processes may share an account, but this does not constitute OS-level privilege separation. Before production, separate the release account and grant only required access to artifacts, SQLite and product repositories; validate directory and WAL permissions. The example service is a baseline, not an unattended installer.

Start API, build lane and deploy lane as separate services. Never start multiple API installations against the same data path on different machines. Restart services after config changes. `factory doctor` checks installed/configured components; it does not authenticate to the model provider or certify application readiness.

## Data layout

| Path | Contents |
| --- | --- |
| `$FACTORY_DATA/factory.sqlite3` | Workflow state: projects, plans, jobs, events, workers, deployments |
| `$FACTORY_DATA/products/<project-id>/` | **Product git repository** — source, history and branches |
| `$FACTORY_DATA/product-data/<project-id>/` | Durable per-product data (`{data}`): the generated application's SQLite file and uploads. Deliberately **outside** the product repository |
| `$FACTORY_DATA/jobs/<job-id>/` | Per-job evidence, exported source, artifacts, test reports |
| `$FACTORY_DATA/workers/<job-id>/` | Per-worker worktrees (transient; safe to prune when no job is running) |
| `$FACTORY_DATA/edits/<edit-id>/` | Studio draft sources |
| `$FACTORY_DATA/memory/<project-id>/` | Optional Memorix project memory |
| `$FACTORY_DATA/agent-runtime/` | Temporary per-worker homes created and removed per invocation |

The product repository under `products/` is the durable record of a client's code. Back it up with the same care as SQLite: losing it loses history and branches even if artifacts survive. Do not hand-edit a product repository while a job is running; the scheduler checks out branches and creates worktrees in it.

Worker worktrees may be left behind by a crash. They are safe to remove when no job is running: `git -C "$FACTORY_DATA/products/<id>" worktree prune`. If a branch is deleted but a worktree still points at it, git refuses to recreate that branch — remove the stale worktree first.

## Verification profiles

Profiles are administrator-controlled.

**Native (default for the Nuxt profile).** `commands` is an ordered list of argv arrays run in a disposable copy of the product source, with a shared wall-clock deadline and bounded output. `checks_dir` plus `checks_argv` add operator-owned checks that run after the build; `{checks}` in an argument expands to the resolved checks directory. A relative `checks_dir` (the shipped default is `"checks"`) is resolved against the checkout that contains the `factory` package, never against the working directory of whichever process started the API or a scheduler — do not make it CWD-dependent, because that silently runs no operator checks while still looking validated. `checks_dir` without `checks_argv` is rejected at startup: a directory with no checks runs nothing while
looking validated, which is exactly how the native profile once shipped with no operator checks at all.
`artifact_dir` names the directory exported as the artifact. `serve_argv`, `readiness_path` and `qa_timeout`
drive the tester run. Native execution requires `FACTORY_ALLOW_NATIVE=1` and is **not** a sandbox.

Keep the checkout path free of spaces. Nuxt Content percent-encodes a space into `%20` and then cannot resolve its own path, so a build in `.../Default Project/...` succeeds while the rendered page and browser acceptance cases fail. This is not a space-escaping bug in the factory's argv handling; the argv arrays are already passed as arrays.

**Persistence and `env`.** A native profile may declare `env`, a map of uppercase names to values, which reaches the build, the operator checks and the served application. Two placeholders expand: `{data}` is the product's durable data directory and `{app}` is the build tree. The shipped default is `{"DATABASE_URL": "file:{data}/app.db", "DATA_DIR": "{data}"}`. Validation rejects unknown placeholders, lowercase names, and every `FACTORY_*` name or provider credential — the generated application is the least trusted process in the factory and must never receive a factory token or an API key. Secrets belong in the operator's environment, not in a profile.

Two data directories exist and they are not interchangeable. **Verification** gets a throwaway directory inside its disposable build tree, so a case that creates a record and then lists records sees the same result every run instead of inheriting rows from the previous build. **`factory run <project>`** serves the published artifact against `data/product-data/<id>`, which sits **outside** the product's git working tree: a SQLite file inside the repository could be committed by `git add -A`, would be clobbered by the next checkout, and would ship in the artifact. A generated application may also legitimately own a `data/` directory of its own, so an inside-the-repo path would collide with real source. `app_data_dir()` in `factory/local_app.py` is the single definition; do not recompute the path elsewhere. `serve_argv` resolves against the artifact root, which already corresponds to `artifact_dir`, so a leading `release/` on the entry point is stripped rather than doubled.

The default stack for a new product is a local SQLite file, and the planner and worker briefs say so explicitly. An external database is chosen only when the brief names one (Supabase, Postgres, MySQL, or any hosted database with a hostname and password). The Docker verification profile has no persistent mount, so treat persistence as a native-runner capability.

The shipped checks are `checks/secret_scan.py` and `checks/js_syntax.py`.

- **`secret_scan.py <artifact_dir>`** blocks credential-shaped strings and the exact values of
  configured secrets from appearing in the built artifact. It is a pattern and exact-value scan over
  text files up to 20 MB, not entropy analysis: an obfuscated or split credential will pass.
- **`js_syntax.py [--allow-empty] <artifact_dir>`** runs `node --check` on every `.js`/`.mjs`/`.cjs`
  file in the artifact. `node --check` parses without executing, so generated application code is
  never run by this check. It matters because a build can pass while shipping JavaScript that does
  not parse — a stale bundler cache, a truncated generated file, or a malformed entry point that
  `npm run build` never evaluated. Without `--allow-empty`, an artifact containing no JavaScript is
  a **failure**: a Nuxt artifact with no JS means the path or the layout is wrong, and reporting
  PASS would be a false clearance. Pass `--allow-empty` in a profile whose artifact legitimately
  ships no JavaScript. If `node` is not on PATH both the check and the build fail loudly rather
  than clearing the artifact.

`factory doctor` prints the resolved `checks_dir`, whether it exists, and the exact `checks_argv`
for each profile, so what will run is visible before a build instead of being discovered in the
evidence afterwards.

**Docker (optional, legacy).** Images must be digest pinned, pre-pulled, and contain Python 3 plus any application toolchain. Verification has no network access, so dependencies must be preinstalled in an audited image; do not enable network to make arbitrary generated install scripts work. Checks execute in `/work/app`, see source at `/source` read-only and checks at `/checks` read-only, and must export deployable files to `/output`. For Docker profiles `/output` is the export contract and `artifact_dir` is not used.

`npm ci` requires network access, which is why the Nuxt Studio profile is native rather than Docker. Add application-specific acceptance tests, not just build checks. The included static check is structural; it is not browser, accessibility, security or business acceptance certification.

## Concurrency and resource bounds

- `FACTORY_WORKER_CONCURRENCY` (default 3, max 16) bounds how many Pi harness workers run at once. Each is its own process tree with its own worktree and temporary `HOME`.
- Because workers are real model calls, raising this multiplies provider spend and local CPU/RAM. Harness workers are **not** metered by the per-call budget; only planner, review and tester-preparation calls are.
- `FACTORY_AGENT_TIMEOUT` applies per worker. A fixer spawned by a merge conflict is also a full worker run; give it enough time (the default 600s) when conflicts are expected.
- One build scheduler and one deploy scheduler, each holding a flock. Do not run two build schedulers against the same data path.

## Deployment target

Deployment is deferred and hidden in the dashboard. The following documents the legacy helper for maintenance.

Prepare a separate unprivileged account and dedicated root per application/environment. Install `deploy/sf-release` as root-owned executable `/usr/local/bin/sf-release`. Write root-owned `/etc/software-factory-release.json`, for example:

```json
{"root":"/srv/portfolio","health_url":"http://127.0.0.1:8081/","restart_argv":[],"health_attempts":15}
```

Serve `/srv/portfolio/current` with your web server. For a server application, preconfigure a fixed service and a narrowly authorized restart command in `restart_argv`. Keep database and uploads outside release directories. Do not grant generic sudo access. Provision health endpoints and TLS before releasing. The helper checks HTTP 200; add stronger application-specific health tests where required.

In factory config, add targets under `PROJECT_ID:staging` and `PROJECT_ID:production`:

```json
{"host":"release@app.example.com","identity_file":"/secure/release_key","known_hosts":"/secure/known_hosts","helper":"/usr/local/bin/sf-release","port":22}
```

Verify SSH host keys through a trusted channel. Keys belong to the deployment account only. Every environment needs its own app root/account configuration. Staging must use a representative runtime and safe data. Deploy staging from the dashboard, perform human acceptance, then approve the exact digest using the release token.

If SSH is interrupted, inspect the remote `release.json`. Run `sf-release status` on the target. If the journal says `deploying` or `rollback_failed`, investigate and explicitly run `sf-release recover` to restore the previous release. Then run `factory reconcile DEPLOYMENT_ID` from the controller. An unknown first deployment with no previous healthy version requires operator incident handling; the CLI deliberately refuses to invent a successful reconciliation.

## Backup and restore

`factory backup /safe/path/factory.sqlite3` creates a consistent online SQLite snapshot. For a complete backup, stop the schedulers, then copy the verified SQLite snapshot, **the entire `products/` directory**, the `jobs/` and `memory/` directories, and configuration. Encrypt the backup and store it off-host. Back up secrets separately.

Do not back up `workers/` — those are transient worktrees. Do back up `products/`, because it holds the only history of client code, and `product-data/`, because it holds each client's live records (the application's SQLite file) and cannot be reconstructed from source.

Test restoration into an isolated instance: integrity check, project/job history, product repository branches, artifact digest verification, and memory access. Never point a restore drill at production targets. Restore files with correct ownership before starting workers. Interrupted jobs require inspection and explicit approval to retry.

Monitor worker heartbeat age, failed/unknown jobs, memory failures, disk use and backup age. Disk use matters more than before: each worker holds a full worktree plus a dependency install, so a single job can occupy several hundred megabytes. Audit events contain project data; restrict access. Retention is manual in this release: never remove artifacts referenced by active releases, investigations or restore points. Keep disk-space alerts enabled.

## Production acceptance still required

Live Pi generation/review with the selected provider across all stages; a **single `build` job that runs a plan through workers, verification and publication in one attempt** (none has ever completed — every published artifact came from a separately enqueued `verify` job); live Memorix roundtrip; HTTPS and token rotation; actual staging/production SSH deployment; service restart, health failure and rollback; server reboot/interrupted-release recovery; full backup restoration; and application-specific browser/security/business tests. No remote production deployment was performed during implementation.

## Client separation (not implemented)

Products are separated by git repository and directory, not by authentication. There is one operator; clients do not log in. There is no `client_id`, no per-client archiving, and no export or hand-off path for a product repository. Do not describe the current release as multi-tenant, and do not assume you can cleanly delete "everything belonging to client X" without manual work.
