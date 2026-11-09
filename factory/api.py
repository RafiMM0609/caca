from contextlib import asynccontextmanager
import hmac
import re
import json
import shutil
import time
from collections import defaultdict, deque
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import Settings
from .db import Conflict, Store
from .models import DeployInput, Plan, ProjectInput, StrictModel
from .workspace import snapshot
from .studio import Studio


class PlanUpdate(StrictModel):
    revision: int
    plan: Plan


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
    attempts = defaultdict(deque)

    @app.middleware("http")
    async def guards(request: Request, call_next):
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
        response = await call_next(request)
        response.headers.update({
            "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY",
            "Referrer-Policy": "no-referrer", "Cache-Control": "no-store",
            "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; "
                                       "img-src 'self' data: blob:; frame-src http://127.0.0.1:*; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
        })
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

    @app.get("/healthz")
    def health():
        return {"status": "ok"}

    @app.get("/api/status")
    def status(_=Depends(role)):
        pulses = {r["key"]: time.time() - float(r["value"]) < 15 for r in store.all("SELECT * FROM runtime")}
        return {"workers": pulses, "model_configured": bool(settings.model),
                "pi_installed": bool(shutil.which(settings.pi)), "memorix_enabled": bool(settings.memorix),
                "profiles": list(settings.profiles), "targets": list(settings.targets),
                "profile_features": {key: {"studio": value.studio, "tester": value.tester} for key, value in settings.profiles.items()},
                "mode": "single-operator", "version": "0.1.0"}

    @app.get("/api/projects")
    def projects(_=Depends(role)):
        return store.all("SELECT * FROM projects ORDER BY created DESC LIMIT 200")

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

    static = Path(__file__).parent / "static"
    app.mount("/assets", StaticFiles(directory=static), name="assets")

    @app.get("/")
    def index():
        return FileResponse(static / "index.html")

    return app
