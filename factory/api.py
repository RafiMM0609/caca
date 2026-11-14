from contextlib import asynccontextmanager
import hmac
import logging
import re
import json
import shutil
import tarfile
import time
from collections import defaultdict, deque
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware

import anyio

from .config import Settings
from .db import Conflict, Store
from pydantic import Field

from .models import DeployInput, Plan, ProjectInput, StrictModel
from .answers import apply_answers
from .workspace import snapshot
from .studio import Studio
from .stream import EVENTS_SQL, MAX_BATCH, event_frames
from . import observe


class PlanUpdate(StrictModel):
    revision: int
    plan: Plan


class AnswersInput(StrictModel):
    """Operator answers keyed by the question text, exactly as the planner wrote it."""
    revision: int
    answers: dict[str, str] = Field(min_length=1, max_length=30)


def create_app(settings=None):
    settings = settings or Settings.from_env()
    settings.validate()
    store = Store(settings.data / "factory.sqlite3")
    studio = Studio(settings, store)

    @asynccontextmanager
    async def lifespan(app):
        yield
        studio.close()

    app = FastAPI(lifespan=lifespan, title="Anton Software Factory", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store, app.state.settings = store, settings
    # The dashboard polls JSON that is dominated by planner prose, and a plan is ~30 KB per
    # project. Compression is applied to responses only; it never touches what the operator
    # reads in the UI. minimum_size keeps the tiny /healthz and 304-sized bodies unencoded.
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    attempts = defaultdict(deque)

    @app.middleware("http")
    async def guards(request: Request, call_next):
        # Correlation id: echoed to the client so a reported failure can be found in the log
        # without asking the operator to reproduce it.
        request_id = observe.new_request_id(request.headers.get(observe.REQUEST_ID_HEADER))
        request.state.request_id = request_id
        began = time.perf_counter()
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            origin = request.headers.get("origin")
            if origin and origin.rstrip("/") != str(request.base_url).rstrip("/"):
                return JSONResponse({"detail": "Cross-origin writes are forbidden"}, status_code=403)
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 256000:
                    return JSONResponse({"detail": "Request body exceeds limit"}, status_code=413)
            request._body = bytes(body)
        try:
            response = await call_next(request)
        except Exception as exc:
            # A crash must leave a trace with the id the client was given, otherwise the 500 the
            # operator saw is untraceable.
            observe.log_event("http.error", level=logging.ERROR, request_id=request_id,
                              method=request.method, path=request.url.path,
                              error=type(exc).__name__, ms=round((time.perf_counter() - began) * 1000, 1))
            raise
        response.headers.update({
            "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY",
            "Referrer-Policy": "no-referrer", "Cache-Control": "no-store",
            "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; "
                                       "img-src 'self' data: blob:; frame-src http://127.0.0.1:*; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
            observe.REQUEST_ID_HEADER: request_id,
        })
        # The stream is logged once, by its own route, and never per iteration: it reads SQLite
        # every second for the life of the connection, and a line per read is a log nobody can
        # use growing forever on a small disk.
        if "/stream" not in request.url.path:
            observe.log_event(
                "http.request", method=request.method, path=request.url.path,
                status=response.status_code, ms=round((time.perf_counter() - began) * 1000, 1),
                request_id=request_id)
        return response

    def role(request: Request, authorization: str = Header(default="")):
        key = request.client.host if request.client else "unknown"
        now = time.monotonic()
        recent = attempts[key]
        while recent and recent[0] < now - 60:
            recent.popleft()
        if len(recent) >= 20:
            raise HTTPException(429, "Too many failed authentication attempts")
        token = authorization.removeprefix("Bearer ") if authorization.startswith("Bearer ") else ""
        for name, expected in (("operator", settings.operator_token), ("release", settings.release_token)):
            if hmac.compare_digest(token, expected):
                return name
        recent.append(now)
        raise HTTPException(401, "Authentication required", headers={"WWW-Authenticate": "Bearer"})

    def operator(actor=Depends(role)):
        if actor != "operator":
            raise HTTPException(403, "Operator token required")
        return actor

    @app.exception_handler(Conflict)
    async def conflict(_, exc):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(KeyError)
    async def missing(_, exc):
        return JSONResponse({"detail": "Record not found"}, status_code=404)

    @app.exception_handler(ValueError)
    async def invalid(_, exc):
        return JSONResponse({"detail": "Invalid draft or content; inspect the supported content types and size limits"}, status_code=422)

    APP_STARTED = time.time()

    @app.get("/healthz")
    def health():
        """Liveness only: cheap, unauthenticated, and never touches the disk.

        A probe that reads SQLite turns a slow disk into a restart loop, so the dependency check
        lives in /healthz/detail where a human or a monitor asks for it deliberately.
        """
        return {"status": "ok"}

    @app.get("/healthz/detail")
    def health_detail(_=Depends(role)):
        """Readiness with reasons. Authenticated: it names configured components and lane ages,
        which is operator information, not public information."""
        return observe.snapshot(store, settings, started=APP_STARTED)

    @app.get("/api/status")
    def status(_=Depends(role)):
        pulses = {r["key"]: time.time() - float(r["value"]) < 15 for r in store.all("SELECT * FROM runtime")}
        return {"workers": pulses, "model_configured": bool(settings.model),
                "pi_installed": bool(shutil.which(settings.pi)), "memorix_enabled": bool(settings.memorix),
                "profiles": list(settings.profiles), "targets": list(settings.targets),
                "profile_features": {key: {"studio": value.studio, "tester": value.tester} for key, value in settings.profiles.items()},
                "mode": "single-operator", "version": "0.1.0"}

    # Columns the project list actually renders. `plan` is deliberately absent: it was 91% of
    # this response (measured: 294 KB over 9 projects, 270 KB of it plan JSON) and the list view
    # never reads it — the detail route is what returns the plan. `brief` is truncated because
    # the card shows a 170-character preview. Adding a column here is a UI decision, so keep
    # the projection explicit rather than SELECT *.
    LIST_COLUMNS = "id,name,substr(brief,1,200) AS brief,profile,state,revision,created,client_id"

    @app.get("/api/projects")
    def projects(_=Depends(role)):
        return store.all(f"SELECT {LIST_COLUMNS} FROM projects ORDER BY created DESC LIMIT 200")

    @app.post("/api/projects", status_code=201)
    def create_project(payload: ProjectInput, _=Depends(operator)):
        if payload.profile not in settings.profiles:
            raise HTTPException(422, "Profile is not configured by the server operator")
        return store.create_project(payload)

    @app.get("/api/projects/{project}")
    def project_detail(project: str, _=Depends(role)):
        result = store.project(project)
        result["jobs"] = store.all("SELECT * FROM jobs WHERE project_id=? ORDER BY created DESC LIMIT 100", (project,))
        # The workers table is the record of what the planner assigned and what each worker
        # did. Without it the dashboard could show plans but never the parallel work.
        result["workers"] = store.all("SELECT * FROM workers WHERE project_id=? ORDER BY created", (project,))
        result["deployments"] = store.all("SELECT * FROM deployments WHERE project_id=? ORDER BY created DESC", (project,))
        result["served"] = store.served(project)
        return result

    @app.post("/api/projects/{project}/plan", status_code=202)
    def plan(project: str, _=Depends(operator)):
        if not settings.model or not shutil.which(settings.pi):
            raise HTTPException(503, "Configure Pi and FACTORY_MODEL before requesting an AI plan")
        return store.enqueue(project, "plan", allowed={"draft", "awaiting_plan", "ready", "failed", "cancelled", "interrupted"},
                             next_state="planning")

    @app.put("/api/projects/{project}/plan")
    def update_plan(project: str, payload: PlanUpdate, _=Depends(operator)):
        with store.tx() as db:
            row = db.execute("SELECT * FROM projects WHERE id=?", (project,)).fetchone()
            if not row:
                raise KeyError(project)
            if row["revision"] != payload.revision:
                raise Conflict("Plan revision changed; reload before editing")
            if db.execute("SELECT 1 FROM jobs WHERE project_id=? AND state IN ('running','queued')", (project,)).fetchone():
                raise Conflict("Cannot edit plan while a job is active")
            db.execute("UPDATE projects SET plan=?,revision=revision+1,state='awaiting_plan' WHERE id=?",
                       (payload.plan.model_dump_json(), project))
            store.event_tx(db, project, None, "plan.edited", {"revision": payload.revision + 1})
        return store.project(project)

    @app.post("/api/projects/{project}/answers")
    def answer_questions(project: str, payload: AnswersInput, _=Depends(operator)):
        """Resolve planner questions from the UI instead of hand-editing plan JSON.

        Applying an answer in one operation is the point: it must reach the assumption record,
        every task instruction and every test flow. An operator editing JSON had to remember
        all three, and forgetting the test flows once failed a build whose application was
        correct.
        """
        record = store.project(project)
        if not record["plan"]:
            raise HTTPException(404, "This project has no plan to answer")
        plan = Plan.model_validate_json(record["plan"])
        try:
            updated = apply_answers(plan, payload.answers)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        if updated == plan:
            raise HTTPException(422, "No answers were applied; answer at least one question")
        with store.tx() as db:
            row = db.execute("SELECT revision FROM projects WHERE id=?", (project,)).fetchone()
            if not row:
                raise KeyError(project)
            if row["revision"] != payload.revision:
                raise Conflict("Plan revision changed; reload before answering")
            if db.execute("SELECT 1 FROM jobs WHERE project_id=? AND state IN "
                          "('running','queued')", (project,)).fetchone():
                raise Conflict("Cannot edit plan while a job is active")
            db.execute("UPDATE projects SET plan=?,revision=revision+1,state='awaiting_plan' "
                       "WHERE id=?", (updated.model_dump_json(), project))
            store.event_tx(db, project, None, "plan.answered",
                           {"revision": payload.revision + 1,
                            "answered": sorted(payload.answers),
                            "remaining": updated.questions})
        return store.project(project)

    @app.post("/api/projects/{project}/approve", status_code=202)
    def approve(project: str, revision: int = Query(ge=1), _=Depends(operator)):
        record = store.project(project)
        profile = settings.profiles[record["profile"]]
        if profile.tester and (not record["plan"] or not Plan.model_validate_json(record["plan"]).test_flows):
            raise HTTPException(422, "This profile requires planner API/browser test flows before approval")
        return store.enqueue(project, "build", {"revision": revision},
                             allowed={"awaiting_plan", "failed", "cancelled", "interrupted"}, next_state="building")

    @app.post("/api/projects/{project}/deploy", status_code=202)
    def deploy(project: str, payload: DeployInput, actor=Depends(role)):
        if payload.environment == "production" and actor != "release":
            raise HTTPException(403, "Production approval requires the separate release token")
        if project + ":" + payload.environment not in settings.targets:
            raise HTTPException(422, "Configure this project's explicit deployment target on the server")
        return store.enqueue(project, "deploy", payload.model_dump(), allowed={"ready"})

    @app.post("/api/jobs/{job}/cancel", status_code=202)
    def cancel(job: str, _=Depends(operator)):
        store.cancel(job)
        return {"status": "cancellation_requested"}

    @app.get("/api/projects/{project}/events")
    def events(project: str, after: int = Query(default=0, ge=0), _=Depends(role)):
        store.project(project)
        return store.all("SELECT * FROM events WHERE project_id=? AND seq>? ORDER BY seq LIMIT 300", (project, after))

    @app.get("/api/projects/{project}/stream")
    async def stream(request: Request, project: str, after: int = Query(default=0, ge=0),
                     _=Depends(role)):
        """Server-sent events for one project, resumable from a cursor.

        `async def` is mandatory, not stylistic: a synchronous endpoint runs in anyio's worker
        thread pool, which is capped at 40 threads. One long-lived stream would occupy one of
        those threads for its whole life, so a handful of open dashboard tabs would starve every
        other request. This endpoint awaits instead of blocking.

        The SQLite read is blocking, so it runs in a worker thread — `anyio.to_thread.run_sync`
        — rather than directly. That keeps the read off the event loop without holding an async
        task hostage for the duration of a disk read.
        """
        store.project(project)

        async def fetch(cursor):
            return await anyio.to_thread.run_sync(
                lambda: store.all(EVENTS_SQL, (project, cursor, MAX_BATCH)))

        return StreamingResponse(
            event_frames(fetch, project, after),
            media_type="text/event-stream",
            headers={
                # Proxies must not buffer an event stream. Caddy in particular buffers
                # `reverse_proxy` responses by default; the deployment note records the
                # `flush_interval -1` directive this header cannot substitute for.
                "Cache-Control": "no-store",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    @app.get("/api/jobs/{job}/evidence")
    def evidence(job: str, _=Depends(role)):
        row = store.one("SELECT * FROM jobs WHERE id=?", (job,))
        root = settings.data / "jobs" / row["id"]
        result = {}
        if root.exists():
            for path in sorted(root.iterdir()):
                if path.is_file() and path.suffix in (".json", ".txt"):
                    result[path.name] = path.read_text()[:300000]
        report = root / "testing" / "report.json"
        if report.exists():
            result["testing/report.json"] = report.read_text()
        return result

    @app.get("/api/jobs/{job}/testing/{filename}")
    def testing_image(job: str, filename: str, _=Depends(role)):
        row = store.one("SELECT * FROM jobs WHERE id=?", (job,))
        if not re.fullmatch(r"case-[0-9]+\.png", filename):
            raise HTTPException(404, "Image not found")
        path = settings.data / "jobs" / row["id"] / "testing" / filename
        if not path.is_file():
            raise HTTPException(404, "Image not found")
        return FileResponse(path, media_type="image/png")

    def local_operator(request: Request, actor=Depends(operator)):
        if not request.client or request.client.host not in ("127.0.0.1", "::1"):
            raise HTTPException(403, "Studio dev editing is local-only; use the factory on localhost")
        host = request.url.hostname
        if host not in ("127.0.0.1", "localhost", "::1", "testserver"):
            raise HTTPException(403, "Studio dev editing requires a localhost dashboard")
        return actor

    @app.get("/api/projects/{project}/studio")
    def studio_state(project: str, _=Depends(role)):
        record = store.project(project)
        return {**studio.state(project), "enabled": settings.profiles[record["profile"]].studio}

    @app.post("/api/projects/{project}/studio/start", status_code=202)
    def studio_start(project: str, _=Depends(local_operator)):
        return studio.start(project)

    @app.post("/api/projects/{project}/studio/stop")
    def studio_stop(project: str, _=Depends(local_operator)):
        return studio.stop_preview(project)

    @app.get("/api/projects/{project}/studio/diff")
    def studio_diff(project: str, _=Depends(operator)):
        store.project(project)
        return studio.diff(project)

    @app.post("/api/projects/{project}/studio/apply", status_code=202)
    def studio_apply(project: str, _=Depends(local_operator)):
        return studio.apply(project)

    @app.post("/api/projects/{project}/studio/discard")
    def studio_discard(project: str, _=Depends(local_operator)):
        return studio.discard(project)

    @app.post("/api/projects/{project}/studio/starter", status_code=202)
    def studio_starter(project: str, _=Depends(local_operator)):
        return studio.starter(project)

    @app.get("/api/projects/{project}/source")
    def source(project: str, _=Depends(role)):
        artifact = json.loads(store.project(project)["artifact"] or "null")
        if not artifact:
            raise HTTPException(404, "No verified source yet")
        return snapshot(settings.data / "jobs" / artifact["job_id"] / "source")

    @app.get("/api/projects/{project}/artifact")
    def artifact_download(project: str, _=Depends(role)):
        artifact = json.loads(store.project(project)["artifact"] or "null")
        if not artifact:
            raise HTTPException(404, "No verified artifact yet")
        return FileResponse(settings.data / "jobs" / artifact["job_id"] / "artifact.tar.gz",
                            filename=f"{project}-{artifact['digest'][:12]}.tar.gz", media_type="application/gzip")

    @app.post("/api/projects/{project}/serve", status_code=202)
    def start_serving(project: str, _=Depends(operator)):
        """Run the project's verified build output on a local port.

        Refused before the job is queued when there is nothing to serve or the profile cannot
        be started: learning that from a failed job is slower and less clear than being told
        which field is missing.
        """
        from . import serving as serving_app

        record = store.project(project)
        if not record["artifact"]:
            raise HTTPException(422, "This project has no verified artifact to serve")
        profile = settings.profiles[record["profile"]]
        ok, reason = serving_app.servable(profile)
        if not ok:
            raise HTTPException(422, reason)
        artifact = json.loads(record["artifact"])
        return store.enqueue(project, "serve", {"digest": artifact["digest"]},
                             allowed={"ready", "failed", "cancelled", "interrupted", "awaiting_plan"},
                             next_state="serving")

    @app.post("/api/projects/{project}/stop")
    def stop_serving(project: str, _=Depends(operator)):
        """Stop a served app. The serve lane owns the process, so it is asked through a row
        rather than killed from here: the dashboard process must not hold a handle it cannot
        see the consequences of."""
        row = store.served(project)
        if row is None:
            raise HTTPException(404, "This project is not being served")
        store.forget_served(project)
        store.event(project, None, "serve.stop_requested", {})
        with store.tx() as db:
            db.execute("UPDATE projects SET state=? WHERE id=?", ("ready", project))
        return {"state": "stopping"}

    @app.get("/api/apps")
    def running_apps(_=Depends(operator)):
        """What is running, so a tunnel can be pointed at a port."""
        apps = []
        for row in store.served_apps():
            record = store.project(row["project_id"])
            apps.append({"project": row["project_id"], "name": record["name"],
                         "port": row["port"], "url": f"http://127.0.0.1:{row['port']}",
                         "state": row["state"], "digest": row["digest"],
                         "restarts": row["restarts"], "started": row["started"]})
        return {"apps": apps}

    @app.get("/api/projects/{project}/export")
    def project_export(project: str, _=Depends(operator)):
        """Download the product's whole repository, for handing to a client.

        The artifact endpoint ships the current source; this ships the history as a git
        bundle, which a client can `git clone` directly. Operator only: it exposes every
        branch, including abandoned work.
        """
        from . import products as product_repo

        record = store.project(project)
        root = product_repo.product_root(settings.data, record["id"])
        if not root.is_dir():
            raise HTTPException(404, "This product has no repository yet")
        bundles = settings.data / "exports"
        bundles.mkdir(parents=True, exist_ok=True, mode=0o700)
        destination = bundles / f"{record['id']}.bundle"
        try:
            receipt = product_repo.bundle_repository(root, destination)
        except product_repo.GitError as exc:
            raise HTTPException(500, f"Could not bundle the repository: {str(exc)[:200]}") from exc
        branches = product_repo.bundle_heads(root)
        store.event(record["id"], None, "project.exported",
                    {"bytes": receipt["bytes"], "digest": receipt["digest"],
                     "branches": branches, "client_id": record.get("client_id", "")})
        label = record.get("client_id") or record["id"][:12]
        return FileResponse(destination,
                            filename=f"{label}-{record['name'][:40].replace(' ', '-')}.bundle",
                            media_type="application/octet-stream")

    # Client attribution is a label, not a login: these endpoints are operator-only, and
    # everything they return is what the single-project export already returns, just for
    # every project of one client at once.
    CLIENT_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,79}$")

    @app.get("/api/clients/{client_id}/inventory")
    def client_inventory(client_id: str, _=Depends(operator)):
        """Everything attributed to one client, so retirement is mechanical, not manual."""
        from . import products as product_repo

        if not CLIENT_ID.fullmatch(client_id):
            raise HTTPException(422, "Invalid client id")
        rows = store.client_projects(client_id)
        if not rows:
            raise HTTPException(404, "No product is attributed to this client")
        projects = []
        for row in rows:
            root = product_repo.product_root(settings.data, row["id"])
            projects.append({"project": row["id"], "name": row["name"],
                             "profile": row["profile"], "state": row["state"],
                             "revision": row["revision"], "created": row["created"],
                             "repository": root.is_dir()})
        return {"client_id": client_id, "projects": projects}

    @app.get("/api/clients/{client_id}/archive")
    def client_archive(client_id: str, _=Depends(operator)):
        """Download every product of one client, history included, as one archive.

        Each project ships the same cloneable git bundle the single-project export ships,
        plus a manifest naming what is inside and what is absent. A project that has never
        built has no repository, so its manifest entry says so instead of vanishing: an
        archive that silently omits a project would be read as a complete hand-off.
        An oversized repository fails the whole request loudly, for the same reason.
        """
        from . import products as product_repo

        if not CLIENT_ID.fullmatch(client_id):
            raise HTTPException(422, "Invalid client id")
        rows = store.client_projects(client_id)
        if not rows:
            raise HTTPException(404, "No product is attributed to this client")
        exports = settings.data / "exports"
        exports.mkdir(parents=True, exist_ok=True, mode=0o700)
        archive_path = exports / f"client-{client_id}.tar.gz"
        if archive_path.exists():
            archive_path.unlink()
        entries = []
        try:
            for row in rows:
                entry = {"project": row["id"], "name": row["name"], "state": row["state"],
                         "revision": row["revision"], "bundle": None}
                root = product_repo.product_root(settings.data, row["id"])
                if root.is_dir():
                    destination = exports / f"{row['id']}.bundle"
                    receipt = product_repo.bundle_repository(root, destination)
                    entry["bundle"] = {**receipt, "branches": product_repo.bundle_heads(root)}
                entries.append(entry)
        except product_repo.GitError as exc:
            archive_path.unlink(missing_ok=True)
            raise HTTPException(500, f"Could not bundle a repository for this client: "
                                     f"{str(exc)[:200]}") from exc
        manifest = {"client_id": client_id, "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "projects": entries}
        manifest_path = exports / f"client-{client_id}-manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2))
        try:
            with tarfile.open(archive_path, "w:gz") as tar:
                tar.add(manifest_path, arcname=f"client-{client_id}/manifest.json")
                for entry in entries:
                    if entry["bundle"] is None:
                        continue
                    tar.add(exports / f"{entry['project']}.bundle",
                            arcname=f"client-{client_id}/{entry['project']}.bundle")
        finally:
            manifest_path.unlink(missing_ok=True)
        store.event(rows[0]["id"], None, "client.archived",
                    {"client_id": client_id, "projects": [e["project"] for e in entries],
                     "bytes": archive_path.stat().st_size})
        return FileResponse(archive_path, filename=f"{client_id}-archive.tar.gz",
                            media_type="application/gzip")

    static = Path(__file__).parent / "static"
    app.mount("/assets", StaticFiles(directory=static), name="assets")

    @app.get("/favicon.ico")
    def favicon():
        """Without this every page load logs a 404 in the console, which hides real errors."""
        return FileResponse(static / "favicon.svg", media_type="image/svg+xml")

    @app.get("/")
    def index():
        return FileResponse(static / "index.html")

    return app
