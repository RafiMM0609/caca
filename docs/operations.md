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
`artifact_dir` names the directory the **build sanity check** looks for: it must exist and be
non-empty, which proves the configured commands actually produced output. It is *not* what gets
published. `serve_argv`, `readiness_path` and `qa_timeout`
drive the tester run. Native execution requires `FACTORY_ALLOW_NATIVE=1` and is **not** a sandbox.

Keep the checkout path free of spaces. Nuxt Content percent-encodes a space into `%20` and then cannot resolve its own path, so a build in `.../Default Project/...` succeeds while the rendered page and browser acceptance cases fail. This is not a space-escaping bug in the factory's argv handling; the argv arrays are already passed as arrays.

**Persistence and `env`.** A native profile may declare `env`, a map of uppercase names to values, which reaches the build, the operator checks and the served application. Two placeholders expand: `{data}` is the product's durable data directory and `{app}` is the build tree. The shipped default is `{"DATABASE_URL": "file:{data}/app.db", "DATA_DIR": "{data}"}`. Validation rejects unknown placeholders, lowercase names, and every `FACTORY_*` name or provider credential — the generated application is the least trusted process in the factory and must never receive a factory token or an API key. Secrets belong in the operator's environment, not in a profile.

**What the artifact is.** The published artifact is the product's **codebase**, not its build
output: every source file, a generated `README.md`, and a fresh single-commit git history
created by `git init` inside the artifact directory. Build output (`node_modules/`, `release/`,
`.nuxt/`) is excluded, because `npm ci && npm run build` regenerates it from the locked
manifest, it is machine-specific, and shipping it produced a 929-file, 11 MB tarball for a
nine-file application.

Why a fresh `git init` rather than a copy of the product's `.git`: a hand-off should not carry
the operator's branches, stale worktree pointers, abandoned experiments or any remote
configuration. The commit is made with the factory identity, hooks disabled and gpg signing
off, and it cannot contain a credential because `.git/config` is written by `git init` itself.

The README is rendered from the approved plan and the profile, not written by a model. It
documents the exact build commands, the run command, the environment variables the app expects
and what the project was asked to do. A README that invents an install step is worse than none,
and every value it contains is something the factory already knows.

`factory run <project>` is build-on-demand: it extracts the artifact into
`$FACTORY_DATA/runs/<project-id>/`, runs the profile's `commands` there, then serves the result
against the durable data directory. The artifact itself is never mutated.

Symlinks are permitted in an artifact because a production build legitimately emits them
(Nitro links its server bundle's dependencies into a shared `.nitro/` directory). They are
stored as links in the tarball rather than dereferenced, so a shared bundle is not duplicated.
A link whose target is absolute or resolves outside the artifact is rejected at packaging time
and again at extraction, so an artifact can never point at `/etc/passwd` or an operator's home.

Two data directories exist and they are not interchangeable. **Verification** gets a throwaway directory inside its disposable build tree, so a case that creates a record and then lists records sees the same result every run instead of inheriting rows from the previous build. **`factory run <project>`** serves the published artifact against `data/product-data/<id>`, which sits **outside** the product's git working tree: a SQLite file inside the repository could be committed by `git add -A`, would be clobbered by the next checkout, and would ship in the artifact. A generated application may also legitimately own a `data/` directory of its own, so an inside-the-repo path would collide with real source. `app_data_dir()` in `factory/local_app.py` is the single definition; do not recompute the path elsewhere. `serve_argv` resolves against the artifact root, which already corresponds to `artifact_dir`, so a leading `release/` on the entry point is stripped rather than doubled.

The default stack for a new product is a local SQLite file, and the planner and worker briefs say so explicitly. An external database is chosen only when the brief names one (Supabase, Postgres, MySQL, or any hosted database with a hostname and password). The Docker verification profile has no persistent mount, so treat persistence as a native-runner capability.

The shipped checks are `checks/secret_scan.py`, `checks/js_syntax.py` and `checks/js_typecheck.py`.

- **`secret_scan.py <artifact_dir>`** blocks credential-shaped strings and the exact values of
  configured secrets from appearing in the built artifact. It is a pattern and exact-value scan over
  text files up to 20 MB, not entropy analysis: an obfuscated or split credential will pass.
- **`ui_lint.py <artifact_dir>`** rejects the mechanical subset of design defects: `transition: all`,
  `ease-in` on a transition, a duration with no timing function, `outline: none` with no
  `:focus-visible` replacement, `user-select: none` alongside body text, and `<html>` with no
  `lang`. It reports `file:line: rule` and exits 1; an artifact with nothing to scan also exits 1,
  because a gate that sees nothing must not report success. **It does not judge taste** — spacing,
  hierarchy and colour are not regex-detectable, and a check that pretended otherwise would
  produce false failures until operators learned to ignore it. The Perpustakaan Mini artifact,
  whose generic look motivated the check, passes it outright.
- **`js_syntax.py [--allow-empty] <artifact_dir>`** runs `node --check` on every `.js`/`.mjs`/`.cjs`
  file in the artifact. `node --check` parses without executing, so generated application code is
  never run by this check. It matters because a build can pass while shipping JavaScript that does
  not parse — a stale bundler cache, a truncated generated file, or a malformed entry point that
  `npm run build` never evaluated. Without `--allow-empty`, an artifact containing no JavaScript is
  a **failure**: a Nuxt artifact with no JS means the path or the layout is wrong, and reporting
  PASS would be a false clearance. Pass `--allow-empty` in a profile whose artifact legitimately
  ships no JavaScript. If `node` is not on PATH both the check and the build fail loudly rather
  than clearing the artifact.
- **`js_typecheck.py [--require-typecheck] <checkout>`** runs the application's own
  declared typecheck — `npm run typecheck` as an argv array in the tree — so an app can no
  longer ship because it *builds* while carrying type errors. A `typecheck` script is
  opt-in by the application: without one the check exits 0 but prints a visible
  "SKIP: no typecheck script" note, and with `--require-typecheck` it fails instead, for
  profiles that want to enforce it. Missing `npm`/`node` or an unreadable `package.json`
  fail loudly, same rule as `js_syntax`. The pinned Nuxt starter now ships
  `vue-tsc --noEmit` as its `typecheck` script, so starter-derived apps are gated by default.

`factory doctor` prints the resolved `checks_dir`, whether it exists, and the exact `checks_argv`
for each profile, so what will run is visible before a build instead of being discovered in the
evidence afterwards.

**Docker (optional, legacy).** Images must be digest pinned, pre-pulled, and contain Python 3 plus any application toolchain. Verification has no network access, so dependencies must be preinstalled in an audited image; do not enable network to make arbitrary generated install scripts work. Checks execute in `/work/app`, see source at `/source` read-only and checks at `/checks` read-only, and must export deployable files to `/output`. For Docker profiles `/output` is the export contract and `artifact_dir` is not used.

For JSON responses the tester offers `json_contains` (subset: named fields must match, extra response fields are ignored) and `json_equals` (the response must be identical). Prefer `json_contains`: a partial `json_equals` fails a correct application that returns more fields than the case named, and the repair loop will then spend a worker editing correct code. A case that is provably unsatisfiable is reported as a `contract.defect` and fails the job without a repair attempt — fix the case with `json_contains` and re-approve.

`npm ci` requires network access, which is why the Nuxt Studio profile is native rather than Docker. Add application-specific acceptance tests, not just build checks. The included static check is structural; it is not browser, accessibility, security or business acceptance certification.

## Timeouts and run budgets

Three bounds, each for a different unit of work, because one number cannot serve them:

| Variable | Default | Bounds |
| --- | --- | --- |
| `FACTORY_AGENT_TIMEOUT` | 600s | one tool-free structured call (planner, tester preparation, review) |
| `FACTORY_WORKER_TIMEOUT` | 1800s | one Pi harness worker, which may edit a tree and build it |
| `FACTORY_SKILL_WORKER_TIMEOUT` | 3600s | the same, for a task that declares a vendored design skill |
| `FACTORY_JOB_TIMEOUT` | 7200s | **floor** for a whole job |
| `FACTORY_MAX_JOB_TIMEOUT` | 21600s | hard cap for the scaled estimate |

A task that carries a skill gets the longer clock because design work is a loop — build, look at
the page, adjust — and a flat bound cut that loop off mid-iteration. See
[worker wall-clock](knowledge/worker-wall-clock.md) for the measurement. A task without skills
keeps the tighter bound it has no reason to exceed.

`profile.timeout` (default 900s) bounds one verification pass: the build, the operator checks
and every acceptance case. `profile.qa_timeout` (default 120s) is **per acceptance case**, not
for the suite.

`FACTORY_JOB_TIMEOUT` is a floor, not the real ceiling. The engine scales the budget with the
plan: waves, not tasks, drive wall time because a wave runs concurrently, so the estimate is
`batches x worker_timeout + verification + repair allowance`, where a wave wider than
`FACTORY_WORKER_CONCURRENCY` counts as several batches. The configured floor wins when it is
larger, and `FACTORY_MAX_JOB_TIMEOUT` caps the result so a pathological plan cannot claim an
unbounded run. A flat ceiling was wrong in both directions: it killed a legitimate larger plan
with no defect to find, and it let a wedged job run for hours.

Every job records `job.budget` when it starts and `job.timing` when it succeeds:

```json
{"total_seconds": 542.7, "budget_seconds": 7200, "used_fraction": 0.075,
 "waves": [{"tasks": ["project-scaffold"], "seconds": 300.8},
           {"tasks": ["content-and-page"], "seconds": 122.2}],
 "worker_seconds": {"project-scaffold": 253.4, "content-and-page": 105.4},
 "verification_seconds": 118.9}
```

That breakdown is the point: a total alone is not actionable, because 15 minutes of workers
needs a different response from 15 minutes of acceptance tests. Each `wave.done` also carries
its own duration, and each worker's `detail.seconds` carries its harness run time, so a slow
stage is visible without reconstructing it from timestamps.

## Concurrency and resource bounds

- Every harness run — an implementation worker, a conflict fixer and a repair worker — is bounded by `FACTORY_WORKER_TIMEOUT` (default 1800s), not by `FACTORY_AGENT_TIMEOUT` (600s, which bounds the cheap tool-free JSON calls). A run that hits its deadline is reported with the limit it hit and the knob to raise, and it may leave uncommitted edits in the product repository: inspect `git status` there before retrying.
- An implementation worker whose task declares a vendored skill is bounded by `FACTORY_SKILL_WORKER_TIMEOUT` (default 3600s) instead, because design work is a build-look-adjust loop. A worker that fails without a review verdict — a deadline, a crash — now carries a note into the next attempt, so it does not restart with a prompt identical to the one that just failed.
- A worker can launch a browser: `PLAYWRIGHT_BROWSERS_PATH` points at the shared Playwright directory when it exists, so a design task inspects its own page instead of installing a second browser. `HOME` stays per-worker and `~/.ssh` stays unreachable; only that one path is added. `FACTORY_PLAYWRIGHT_BROWSERS` overrides the location.
- `FACTORY_WORKER_CONCURRENCY` (default 3, max 16) bounds how many Pi harness workers run at once. Each is its own process tree with its own worktree and temporary `HOME`.
- Every harness worker run reserves `FACTORY_WORKER_COST` (default 0.25, `0` disables metering) against `jobs.reserved_usd` before it starts, transactionally, so concurrent workers cannot collectively exceed `FACTORY_MAX_JOB_COST`. A run whose reservation would breach the ceiling fails loudly with a terminal worker row and a `worker.budget_exhausted` event. Raising concurrency therefore multiplies provider spend **and** consumes the job budget faster.
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

## Serving a built product

A verified artifact can be run on a local port and exposed through the tunnel that already
serves other applications.

```sh
factory api                              # dashboard
factory scheduler --lane serve           # owns served processes
```

**This does not build anything.** The verifier already ran `npm ci` and `npm run build` and kept
the result at `data/jobs/<job>/output/`. Serving copies that to `data/served/<project>/` and
starts `profile.serve_argv` there.

**A copy, not a symlink.** `Engine.verify` deletes `output/` before the next verification of the
same project, so an app served from there would die on rebuild and a symlink would dangle.

**One process serves both halves.** The product is Nuxt SSR: `/` is rendered from
`server/chunks/`, `/api/*` is handled from the same bundle, and `/_nuxt/*` is static from
`public/`. There is no separate frontend to deploy and no second port.

| Endpoint | Purpose |
| --- | --- |
| `POST /api/projects/<id>/serve` | start; 422 naming the missing field if the profile cannot be served |
| `POST /api/projects/<id>/stop` | stop; the serve lane observes the removed row and releases the process |
| `GET /api/apps` | port and URL per running app — this is what a hostname is pointed at |

Only a profile with **both** `artifact_dir` and `serve_argv` can be served. Today that is
`nuxt-studio`; `node` has no `serve_argv` and `static` has neither.

**Health:** every 30 s the serve lane probes `readiness_path`. One restart is attempted; a second
failure marks the app `unhealthy` and stops retrying. Nothing is paged — a dead app at 03:00
stays dead until someone looks.

**Cloudflare is not configured here.** Add the hostname in Cloudflare and point it at the port
from `GET /api/apps`. The tunnel already serves other applications and its configuration is not
owned by this repository.

**Limits, stated:** one machine serves the factory and every product, so a product that eats RAM
starves the factory; execution is native rather than sandboxed; and a served app is the artifact
that passed at build time, not a continuously verified one.

On a small host, size the factory deliberately. The 2 GB / 2 vCPU machine that recorded the live builds runs `FACTORY_WORKER_CONCURRENCY=1` (the default 3 assumes more memory: two concurrent Node builds do not fit in 2 GiB) and both shipped profiles pass `NODE_OPTIONS=--max-old-space-size=1024` so a Nuxt build's V8 heap is bounded instead of greedy. The job deadline scales with the configured concurrency, so lowering it lengthens the deadline automatically. Bump swap rather than RAM first only as a safety net; a build that thrashes swap is slower but finishes, while an OOM kill leaves an interrupted job to reconcile.

## Client attribution and hand-off

A project may carry an optional `client_id` (lowercase `[a-z0-9][a-z0-9_-]*`, max 80 chars),
recorded on the project row and in the `project.created` event. It is **attribution, not
isolation**: there is still one operator, clients do not log in, and no authorization is
derived from it. What it buys is the ability to tell deliberately which product belongs to
which client, which the eventual hand-off needs.

`GET /api/projects/<id>/export` (operator token) returns a **git bundle** of the whole product
repository — every branch, every commit — rather than a tarball of the working tree. A bundle
clones directly:

```sh
git clone product.bundle my-product
```

The export is recorded as a `project.exported` event with the bundle digest, the branch list
and the `client_id`, so a hand-off is auditable. It contains no remote configuration and no
credential: the product repository has none, and the factory blanks the operator's git
environment when it runs git.

**Still not implemented:** per-client archiving (deleting everything belonging to one client
still needs manual work), and any authorization boundary between clients.

## Client separation (not implemented)

Products are separated by git repository and directory, not by authentication. There is one operator; clients do not log in. There is no `client_id`, no per-client archiving, and no export or hand-off path for a product repository. Do not describe the current release as multi-tenant, and do not assume you can cleanly delete "everything belonging to client X" without manual work.

## Live updates and the reverse proxy

The dashboard keeps one long-lived connection open per viewed project: `GET
/api/projects/<id>/stream` returns server-sent events. Everything else is ordinary
request/response.

This changes what a proxy in front of the API must do.

- **Do not compress `/api`.** Caddy's `encode` buffers a response in order to compress it, and a
  buffered `text/event-stream` is not a stream: live updates stop arriving and the dashboard
  looks frozen until it reconnects. The supplied `deploy/Caddyfile.example` restricts `encode` to
  `/assets/*` for this reason.
- **The application never gzips the stream itself.** `GZipMiddleware` excludes
  `text/event-stream` by content type (it is in Starlette's default exclusion list), and the
  route also sends `X-Accel-Buffering: no`. Those two settings must stay consistent with the
  proxy: if the edge stops compressing `/api` but the app starts compressing the stream (for
  example by narrowing `exclude_content_types`), the same buffering returns.
- **A stream is bounded.** It ends after `MAX_STREAM_SECONDS` (600s) with an
  `event: reconnect` frame, and the client resumes from its cursor. A proxy that silently drops
  idle connections is therefore harmless — the reconnect is the normal path, not an error.
- **Keepalives matter.** An idle stream emits a `: keepalive` comment every 15s. If a proxy
  idle-timeout is shorter than that, raise it rather than lowering the keepalive; a comment-only
  response is cheap.
- **Losing the stream is not an outage.** After three failed reconnect attempts the dashboard
  falls back to cursor polling, which is correct but slower. If an operator reports "updates take
  a few seconds", check the proxy before the browser.

## Logs, health and migrations

### Logs

Every process writes one JSON object per line to stdout, so `journalctl -o cat` and `jq` both
work without a parser. There is one knob: `FACTORY_LOG_LEVEL` (default `INFO`). uvicorn's own
access log is disabled, because the application middleware already emits one `http.request`
record per request and two loggers describing the same request produce both double-sized and
inconsistent logs.

```sh
journalctl -u software-factory-api -o cat | jq 'select(.event=="http.request") | {path,status,ms}'
journalctl -u software-factory-api -o cat | jq 'select(.status>=500)'
```

Two rules that are enforced in code rather than left to discipline:

- **Credentials are redacted by field name**, recursively, at every log call. A token nested in
  a job payload is redacted too. The test asserts that a token cannot appear in the output.
- **The event stream is logged once, never per iteration.** It reads SQLite once a second for the
  life of a connection; a line per read would emit ~3600 lines per hour per open tab for no
  diagnostic value, on a host with 14 GB free.

Volume to expect from the dashboard: roughly 900 lines per hour while a tab is open (one poll
every 8 s, two requests each), about 0.2 MB/h at ~195 bytes per line. A build job adds its own
worker and stage events.

### Health

| Endpoint | Auth | Cost | Purpose |
| --- | --- | --- | --- |
| `/healthz` | none | no I/O at all | Liveness. Never touches SQLite, so a slow disk cannot cause a restart loop. |
| `/healthz/detail` | operator | one indexed query, `/proc` reads | Readiness with reasons: database latency, lane heartbeat ages, configured model and Pi, process RSS and thread count. |

`/healthz/detail` reports `ok`, `degraded` or `fail`, with a severity ladder: once a check has
reported `fail`, a later `degraded` check cannot soften it. A stopped **build** lane is
`degraded` (the API still serves reads and a stopped deploy/serve lane is a configuration
choice); an unreachable database is `fail`.

### Migrations

Schema changes are numbered entries in `MIGRATIONS_LIST` in `factory/db.py`, each applied once and
recorded in `schema_version`. Each migration runs in the same transaction as its version bump, so
a failed migration is not recorded and is retried on the next start instead of leaving the schema
half-changed and forgotten. A migration whose column already exists is adopted and recorded,
which is what makes it safe against a database created by an older release.

Adding one:

```python
(5, "what it changes", "ALTER TABLE some_table ADD COLUMN some_column TEXT"),
```

Do not renumber existing entries, and do not reorder them: versions are explicit, not positional.

### Optional browser test

`tests/test_ui_browser.py` drives the real dashboard in Chromium. It is opt-in and skipped by
default:

```sh
uv run playwright install chromium          # once
FACTORY_UI_TEST=1 uv run pytest tests/test_ui_browser.py -q
```

It is not in the default suite because a Chromium process tree measured ~387 MB resident on a
1.9 GiB host where a build job already uses about 1 GB. Running it *alongside* a build is how the
OOM killer gets involved, and an OOM kill during a build is indistinguishable from a real failure
in the job log. Keep it out of the build lane.

## The Zig profile

`zig-htmx` builds a generated application with `zig build` and serves the resulting binary. It
uses no Node, no `package.json` and no `node_modules`: that is the reason it exists.

**Requirement: `zig` on the scheduler's `PATH`.** The profile invokes `zig` by name, not by an
absolute path, because a checked-in home directory breaks every other checkout. If the service
cannot see Zig, `factory doctor` will not catch it — the failure appears as a build step exiting
non-zero. Set `FACTORY_ZIG=/absolute/path/to/zig` to pin it explicitly, or add its directory to
the unit's `Environment=PATH=...`. `FACTORY_ZIG` is also what the worker's measured toolchain
report reads, so without either setting a worker is told no Zig version and may write code for
the wrong one. Zig pre-1.0 changes std APIs between minor releases; this profile was verified
against **0.15.2**.

Two integration details were required, and both were found by running a real build:

- **`HOME` must exist during verification.** `local_env()` forwards only a small allowlist, so
  the scratch build environment had no `HOME` and `zig build` failed with
  `unable to resolve zig cache directory: AppDataDirUnavailable`. Verification now creates a
  scratch `HOME` inside the throwaway build directory, which also stops one build poisoning the
  next build's cache.
- **A relative binary needs `./`.** `subprocess` searches `PATH`, not the working directory, so
  `serve_argv: ["app"]` never started. Invisible for `node`, which is on `PATH`; fatal for a
  compiled binary. `LocalApp` now prefixes `./` when the file exists and is executable in `cwd`.

**Zig build output is not published.** `zig-out/` and `.zig-cache/` are excluded from the
artifact for the same reason as `node_modules/`: they are regenerated by `zig build`, and
`zig-out/bin/app` is ~2.3 MB of binary that a hand-off must not carry.

**What the profile does not do.** There is no Nuxt Studio equivalent — `studio` is a Nuxt-only
feature. Persistence is file-based by default: the planner is told to write JSON or an
append-only log under `DATA_DIR` rather than link SQLite, because a C dependency is a build risk
a first Zig version should not carry. `checks_argv` deliberately omits `js_syntax.py` (it needs
Node and would report a toolchain problem as an application failure) and keeps
`ui_lint.py` / `ui_moments.py`, which are pure-Python gates over the markup and CSS that a Zig
+ HTMX page still has.

### Measured on this host

| | Zig + HTMX (`zig-htmx`) | Nuxt (`nuxt-studio`) |
| --- | --- | --- |
| Install step | none | `npm ci` (~1 GB peak) |
| Cold build | **21.8 s** (peak 209 MB) | 30–90 s typical |
| Warm build | **0.04 s** | seconds to tens of seconds |
| Served runtime RSS | **3.7 MB** | ~50 MB (Node) |
| Binary / build output | **2.3 MB** | ~300 MB `node_modules` |

A reference application that compiles and serves HTMX fragments ships in
`examples/zig-htmx/`. It documents the two non-obvious API facts a worker gets wrong: `std.http.Server`
handles one connection with no listener (bind `std.net` yourself), and `net.Stream.reader()`
exposes `interface` as a *method* while the writer exposes it as a *field*.
