# Serving a built product — final plan

Written 2026-11-10, revised after measuring what already exists.

## What "deploy" means here

Not the existing `Target`/`Deployer` path. That pushes an artifact over SSH to another host.
This runs the product **on this machine** and exposes it through the Cloudflare tunnel that
already serves other applications on separate subdomains.

## The discovery that replaced a day of work

The verifier **already builds the product and keeps the output**:

```python
# factory/verifier.py:204
built = work / profile.artifact_dir
shipped = list(files_under(built, allow_symlinks=True))
logs.append("BUILD OUTPUT: ... (not published; the artifact is the source tree)")
if output is not None:
    shutil.copytree(built, output, symlinks=True, dirs_exist_ok=True)
```

`engine.py` passes `output`, and it lands at `data/jobs/<job>/output/`. Measured:

| Job | Project | `output/` size |
| --- | --- | --- |
| `e1da2910` | Perpustakaan Enterprise | **3.1 MB** |
| `7da7025f` | Perpustakaan Mini | 11 MB |
| `91ff63db` | Toko Roti Sederhana | 30 MB |

So serving needs **no build at all**: no `npm ci`, no `npm run build`, no 216 MB transient
tree. My earlier estimate of 1.5 days is cut because the runner reduces to copying a few
megabytes and starting a process.

Older outputs are larger because they carry real dependency bundling — the Toko Roti build
ships `better-sqlite3` prebuilds (2.0-2.4 MB per platform, 26 MB total in `server/node_modules`),
after the operator's brief named SQLite. Size varies per product; "3 MB" is a floor, not a rule.
**A serving size estimate must read the previous `output/`, not assume 3 MB.**

## Verified with a real server

Two outputs were started standalone against a live Node process, 30+ minutes after their builds,
from the `output/` directory alone:

- `e1da2910` (Enterprise): `/api/health` → `{"status":"ok"}`, `/api/users` → `{"items":[],"total":0}`
- `(old job)` (Toko Roti): `/api/health` → `{"status":"ok"}`

`server/node_modules` (2.1-26 MB inside `output/`) carries the runtime dependencies, so no
`node_modules` beside the build is needed. This is the answer to whether serving duplicates
storage: the 211 MB is paid once, transiently, inside the verifier's own `TemporaryDirectory`
and never stored.

## Scope

**It applies to a profile only when it defines both `artifact_dir` and `serve_argv`.**

| Profile | `artifact_dir` | `serve_argv` | Servable |
| --- | --- | --- | --- |
| `nuxt-studio` | `release` | `node release/server/index.mjs` | **yes** |
| `node` | `dist` | **none** | no — needs `serve_argv` |
| `static` | **none** | none | no — Docker checks export a source copy to `/output`, not a runnable app |

Every project in this repository is `nuxt-studio`, so every product built so far is servable.
The other two are recorded as gaps rather than silently assumed.

`static` exports via `FACTORY_OUTPUT` inside its Docker check (`checks/static.py` copies the
source into a tarball), so the "built output" there is the source itself and cannot start a
server. Serving a static app would need a general static file server instead of
`profile.serve_argv`.

## Components

1. **Runner** — copy `data/jobs/<published-jobs>/output/` into `data/served/<project>-<rev>/` and
   start `profile.serve_argv` there with `PORT`, `DATA_DIR`, `DATABASE_URL`. A copy rather than a
   symlink: the engine deletes `output/` before its next verification and a symlinked app would
   die or dangle mid-flight. 3-30 MB per app; storage is not the constraint.
2. **Registry** — a `served_apps` table: project, port, url, state, artifact digest, pid,
   started, last health check. The dashboard and the tunnel read it.
3. **Health loop** — every 30s, `GET profile.readiness_path`. Dead → restart once, record it.
   Repeated failures mark the app `unhealthy` and stop retrying. No alerting.
4. **Control endpoints** — `POST /api/projects/<id>/serve`, `POST .../stop`, `GET /api/apps`.
   Operator token. Starting an app whose digest changed copies the newer output instead of
   serving stale code.
5. **Dashboard** — per project: Start/Stop, the port, the URL, the state.

The runner must stop rather than starve: it cannot start an app when the profile lacks
`artifact_dir` or `serve_argv`, and must say which one is missing rather than crashing.

## Deliberate operational model

Two apps at most, one live at a time by SOP, stopped manually by the operator. Not enforced in
code — a hard limit would need to know the machine's capacity and would be wrong on a bigger box.

## Not in this plan

- Cloudflare configuration. The tunnel already serves other apps; adding hostnames needs
  dashboard access this repo does not have. `GET /api/apps` exposes the port so the operator can
  point a hostname at it. The operator's plan is one shared tunnel with several `ingress`
  hostnames, pointing at `localhost:<port>`.
- Environment variable glue beyond what is already in the profile: it should resolve from
  `profile.env` with `{data}`/`{app}` via `expand_env`, not hard-coded names.
- Crash alerting, log shipping, metrics.

## Done when

- Starting a project serves its built `output/` copy and the dashboard shows a reachable URL.
- Stopping releases the port and the process group is gone (no orphan).
- `GET /api/apps` lists what is running, so a tunnel can be pointed at a port.
- A dead app is restarted once and the restart is visible as an event.
- Starting an app with a newer artifact copies the newer output instead of serving stale code.
- A profile without `artifact_dir` or `serve_argv` reports a clear refusal instead of crashing.

## Risks, stated plainly

- **One machine.** The factory and every served product share it. A product that eats RAM
  starves the factory, and the factory is what fixes products.
- **Native, not sandboxed.** `FACTORY_ALLOW_NATIVE` applies; there is no container boundary
  between two served products or between a product and the factory.
- **The artifact is executed as-is.** It passed the verifier, but serving does not re-run the
  acceptance suite. A served app is not continuously verified.
- **`output/` is not archived.** The engine deletes it before the next verification and nothing
  in this plan preserves old builds, so there is no rollback copy beyond what exists.

## Effort

| Step | Estimate |
| --- | --- |
| Runner (copy + spawn + registry) | ~4-5 hours |
| Health loop + restart-once | ~2 hours |
| Control endpoints (`/serve`, `/stop`, `/apps`) | ~2 hours |
| Dashboard controls (Start/Stop, port, URL) | ~3 hours |
| Tests, including the two refusal paths | ~2 hours |
| **Total** | **~1.5 days of focused work** |
