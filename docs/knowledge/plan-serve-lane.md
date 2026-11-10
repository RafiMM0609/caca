# Serving a built product — final plan

Written 2026-11-10. Revised three times as measurement replaced assumption. Nothing implemented.

## What is actually being built

**Not a second build.** Not a deploy to another host. One thing: take the build output the
verifier already produced, put a copy somewhere stable, and keep a Node process running on a
local port so a Cloudflare hostname can point at it.

## What already happens today

Every `build` job already runs `npm ci` and `npm run build`, inside a temporary directory:

```python
# factory/verifier.py:99
with tempfile.TemporaryDirectory(prefix="sf-native-", dir=source.parent) as directory:
    work = Path(directory) / "app"
    clone_tree(source, work)
    # npm ci, npm run build, acceptance tests
```

The result is copied out to `data/jobs/<job>/output/`, and the temporary tree — 211 MB of
`node_modules` — is deleted when the `with` block ends. Nothing is duplicated, and nothing is
left behind.

What persists after a successful build:

| Path | Content | Size |
| --- | --- | --- |
| `data/products/<id>/` | source (git repository) | ~1.4 MB |
| `data/jobs/<job>/output/` | **build output** | 3.1–30 MB |
| `data/jobs/<job>/artifact.tar.gz` | source, for hand-off | ~240 KB |

Measured outputs: Enterprise 3.1 MB, Mini 11 MB, Toko Roti 30 MB. Size varies because the older
build bundles `better-sqlite3` prebuilds for three platforms at 2.0–2.4 MB each. **A serving
estimate must read the previous `output/`, never assume 3 MB.**

## Why a copy is required

`factory/engine.py:347` deletes `output/` before the next verification of the same project:

```python
if output.exists():
    shutil.rmtree(output)
```

An app started from `data/jobs/<job>/output/` would therefore die — or dangle — the moment the
operator rebuilds that project. So serving copies it to a location that owns its own lifecycle:

```
data/jobs/<job>/output/   →  copy  →  data/served/<project>/
```

A symlink is rejected for the same reason plus a second one: the target disappears, and a
dangling link is a worse failure than a copy costing a few megabytes.

**One version is kept.** No rollback archive. If a rebuild is bad, the operator rebuilds again —
which re-runs the acceptance suite, so the new output is verified rather than assumed good.

## Frontend and backend are one process

The generated product is Nuxt SSR. A single Node process serves both:

| Route | Served by |
| --- | --- |
| `/` | SSR-rendered HTML from `server/chunks/` |
| `/api/users` | API route handlers in the same bundle |
| `/_nuxt/*.js` | static assets from `public/` |

Verified live: from `output/` alone, with no `node_modules` beside it, `/api/health` returned
`{"status":"ok"}` and `/api/users` returned `{"items":[],"total":0}`. Two different jobs were
started this way, one of them 30+ minutes after its build.

So there is no separate frontend deploy, no second port, and no second process. One port serves
both, and the Cloudflare hostname points at that one port.

## Profile requirements

A profile is servable only when it defines **both** `artifact_dir` and `serve_argv`.

| Profile | `artifact_dir` | `serve_argv` | Servable |
| --- | --- | --- | --- |
| `nuxt-studio` | `release` | `node release/server/index.mjs` | **yes** |
| `node` | `dist` | *(none)* | no |
| `static` | *(none)* | *(none)* | no — Docker check exports source, not a runnable app |

Every project in this repository uses `nuxt-studio`. The other two are recorded as gaps, and
the runner must refuse with the name of the missing field rather than crash.

## Components

### 1. `served_apps` table

Follows the existing `CREATE TABLE` pattern in `factory/db.py`.

| Column | Meaning |
| --- | --- |
| `project_id` | one row per project; starting again replaces |
| `port` | allocated at start, released at stop |
| `state` | `starting`, `running`, `stopped`, `unhealthy` |
| `digest` | artifact digest the copy was made from |
| `job_id` | which build produced it |
| `pid` | for a stale-row check after a crash |
| `started`, `checked` | timestamps |

### 2. Runner — `factory/serving.py`

- Resolve the published job: the artifact record already holds `job_id` and `digest`.
- Copy `data/jobs/<job_id>/output/` to `data/served/<project_id>/`, replacing any previous copy.
- Start `profile.serve_argv` there using the existing `LocalApp`, which already allocates a free
  port, spawns under `watchdog.py` so the child dies with the parent, injects
  `PORT`/`NITRO_PORT`/`HOST`, and kills the process group on close.
- Resolve `profile.env` through `expand_env` with `data_dir = app_data_dir(settings, project)`,
  so `DATABASE_URL` and `DATA_DIR` point at `data/product-data/<project>/` and survive a restart.
  This reuses the existing helper rather than inventing a second env convention.
- Wait for `profile.readiness_path`, then record `running`.

`LocalApp` lives for one verification today. Serving holds the same object open in the scheduler
process and records it; the gap is lifetime, not machinery.

### 3. Health loop

Every 30 s, request `profile.readiness_path` on each running app.

- Failure → restart once, record `serve.restarted`.
- Failure again within the same run → mark `unhealthy`, stop retrying, record why.

No alerting, no paging. A dead app at 03:00 stays dead until someone looks. That is a deliberate
deferral, not an oversight.

### 4. Job kind and lane

A new `serve` job kind, claimed by its own lane, because the existing claim predicate sends every
non-deploy job to `build`:

```python
# factory/db.py:173
predicate = "kind='deploy'" if lane == "deploy" else "kind!='deploy'"
```

That predicate must gain a `serve` branch, or a serve job will be picked up by the build lane and
run alongside a build. `factory/cli.py` gains `--lane serve`.

A long-lived app does not belong in the build lane, which finishes each job and is expected to
exit. The serve lane owns process lifetime.

### 5. Endpoints

| Endpoint | Behaviour |
| --- | --- |
| `POST /api/projects/<id>/serve` | enqueue a serve job; refuse when no verified artifact |
| `POST /api/projects/<id>/stop` | stop the process group, free the port, mark `stopped` |
| `GET /api/apps` | running apps: project, port, url, state, digest, uptime |

Operator token. `GET /api/apps` is what a tunnel is pointed at.

### 6. Dashboard

Per project with a verified artifact: a Start/Stop control, the local port, the URL, and the
state. No new page — the existing artifact panel gains the controls.

## Cloudflare is out of scope, deliberately

The tunnel already serves other applications on separate subdomains. Adding a hostname needs
dashboard or account access this repository does not have, and the existing token must not be
touched by this work.

The division is:

| This repository | The operator |
| --- | --- |
| Start the app, allocate the port | Add the hostname in Cloudflare |
| Report port and URL at `GET /api/apps` | Point the hostname at `localhost:<port>` |

The operator's model is one shared tunnel with several `ingress` hostnames.

## Operational model

Two apps at most, one live at a time by SOP — the operator stops one before starting another.
Not enforced in code: a hard limit would need to know the machine's capacity and would be wrong
on a larger box.

## Not in this plan

- Rollback archives. One copy per project.
- Crash alerting, log shipping, metrics.
- Serving a profile without `artifact_dir` and `serve_argv`.
- Re-running the acceptance suite against a served app.
- Any change to the existing SSH `Target`/`Deployer` path.

## Done when

- Starting a project copies its verified `output/` and the dashboard shows a reachable URL.
- Stopping releases the port and leaves no orphan process.
- `GET /api/apps` lists running apps with ports, so a tunnel can be pointed at one.
- A killed app is restarted once, and the restart appears as an event.
- Rebuilding a project and starting it again serves the newer output, with the digest updated.
- A profile missing `artifact_dir` or `serve_argv` is refused by name, not by traceback.
- A serve job is never claimed by the build lane.

## Risks, stated plainly

- **One machine.** The factory and every served product share CPU, RAM and disk. A product that
  eats memory starves the factory, and the factory is what fixes products. One-at-a-time is the
  current answer, not a technical limit that was enforced.
- **Native, not sandboxed.** `FACTORY_ALLOW_NATIVE` applies. There is no container boundary
  between two served products, or between a product and the factory.
- **Serving executes what the verifier approved, once.** A served app is not re-verified; it is
  the artifact that passed at build time.
- **No rollback.** A bad rebuild replaces the good copy. The acceptance suite is the guard, and
  it ran before the output was published.
- **The copy is a full directory, not incremental.** A 30 MB product costs 30 MB; two apps cost
  what their outputs weigh.

## Effort

| Step | Estimate |
| --- | --- |
| `served_apps` table + port allocation + stale-row recovery | ~2 h |
| Runner: copy, spawn, readiness, stop | ~4 h |
| Health loop with restart-once | ~2 h |
| `serve` job kind + lane claim branch + CLI flag | ~2 h |
| Endpoints: serve, stop, apps | ~2 h |
| Dashboard controls | ~3 h |
| Tests: refusal paths, lane isolation, restart, replacement | ~3 h |
| **Total** | **~1.5 days** |

First milestone worth stopping at: table + runner + `GET /api/apps`, with a manual start. That
proves the tunnel can be pointed at a port before the dashboard and health loop are built.
