# Serving a built product — plan

Written 2026-11-10. Proposal; nothing implemented.

## What "deploy" means here

Not the existing `Target`/`Deployer` path. That pushes an artifact over SSH to another host.
This runs the product **on this machine** and exposes it through the Cloudflare tunnel that
already serves other applications on separate subdomains.

## What the artifact actually contains

Measured on the Perpustakaan Enterprise artifact: `kind: source`, 30 files, 244 KB, with
`package.json`, `pages/`, `server/`, `.git`. **No `release/`, no `node_modules/`.**

There is no `start:prod` script. The scripts are:

```json
{ "dev": "nuxt dev", "build": "nuxt build", "preview": "node release/server/index.mjs" }
```

So one run is:

```sh
npm ci                                           # ~60-90s
npm run build                                    # produces release/server/index.mjs
DATABASE_URL=file:... node release/server/index.mjs
```

The last command is `profile.serve_argv`, the same value the verifier already uses. Serving must
not invent a second way to start the product.

**Size after a build: ~216 MB per app** (measured). Two live apps fit in the 19 GB free and
1.9 GB RAM this machine has. Ten would not.

## Deliberate operational model

Two apps at most, and **one live at a time by choice**: the operator stops one before starting
another, as an SOP rather than an enforced quota. Not enforced in code, because a hard limit
would need to know the machine's capacity and would be wrong on a bigger box.

No crash alerting. A supervisor restarts a dead app and records it; nobody is paged. That is a
later decision, not this one.

## Reuse, not new machinery

`factory/local_app.py::LocalApp` already does the hard part: picks a free port, spawns under
`watchdog.py` so the child dies with the parent, injects `PORT`/`NITRO_PORT`/`HOST`, waits for
readiness, and kills the process group. `app_data_dir()` already gives each product a
persistent data directory outside its git tree.

The gap is that `LocalApp` lives for one verification and is closed. Serving needs the same
object held open, recorded, and restartable.

## Components

1. **Runner** — copy the artifact into `data/served/<project>/`, run `profile.commands` then
   `serve_argv`. Copy rather than build in place: the artifact is the immutable published
   thing, and re-running it must not mutate it.
2. **Registry** — a `served_apps` table: project, port, url, state, artifact digest, pid,
   started, last health check. The dashboard and the tunnel both read it.
3. **Health loop** — every 30s, `GET profile.readiness_path`. Dead → restart once, record the
   restart. Repeated failures mark the app `unhealthy` and stop retrying.
4. **Control endpoints** — `POST /api/projects/<id>/serve`, `POST .../stop`,
   `GET /api/apps`. Operator token. Starting an app whose artifact digest changed rebuilds.
5. **Dashboard** — per project: a Start/Stop button, the port, the URL, and the state.

## Not in this plan

- Cloudflare configuration. The tunnel already exists and serves other apps; adding hostnames
  needs dashboard access this repository does not have. `GET /api/apps` exposes the port and
  URL so the operator can point a hostname at it.
- TLS termination, custom domains, or a reverse proxy inside the factory.
- Crash alerting, log shipping, metrics.
- Running more than one build of the same project at once.

## Done when

- Starting a project serves its built artifact and the dashboard shows a reachable URL.
- Stopping it releases the port and the process is gone (no orphan).
- `GET /api/apps` lists what is running, so a tunnel can be pointed at a port.
- A dead app is restarted once and the restart is visible as an event.
- Starting an app with a newer artifact rebuilds instead of serving stale code.

## Risks, stated plainly

- **One machine.** The factory and every served product share it. A product that eats RAM
  starves the factory, and the factory is what fixes products.
- **Native, not sandboxed.** `FACTORY_ALLOW_NATIVE` applies; there is no container boundary
  between two served products or between a product and the factory.
- **The artifact is executed as-is.** It passed the verifier, but serving does not re-run the
  acceptance suite. A served app is not continuously verified.
