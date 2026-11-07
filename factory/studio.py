"""Local-only Nuxt Studio draft lifecycle. Never mutate a verified source snapshot."""
import json
import os
import shutil
import threading
import time
from pathlib import Path

from .db import Conflict, uid
from .local_app import LocalApp, local_env
from .models import Plan
from .process import run
from .workspace import clone_tree, files_under


class Studio:
    def __init__(self, settings, store):
        self.settings, self.store = settings, store
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.sessions = {}
        with store.tx() as db:
            db.execute("UPDATE studio_drafts SET state='stopped' WHERE state IN ('preparing','editing')")

    def ensure_local(self):
        if os.environ.get("FACTORY_ALLOW_NATIVE") != "1":
            raise Conflict("Local Studio requires FACTORY_ALLOW_NATIVE=1 and trusted project code")

    def profile(self, project):
        profile = self.settings.profiles[project["profile"]]
        if not profile.studio:
            raise Conflict("This profile does not support Nuxt Studio")
        return profile

    @staticmethod
    def idle(db, project):
        if db.execute("SELECT 1 FROM jobs WHERE project_id=? AND state IN ('queued','running')", (project,)).fetchone():
            raise Conflict("Wait for the active workflow to finish")

    def state(self, project):
        rows = self.store.all("SELECT * FROM studio_drafts WHERE project_id=?", (project,))
        if not rows:
            return {"state": "none"}
        draft = rows[0]
        session = self.sessions.get(project)
        if draft["state"] == "editing" and session and session.get("app"):
            draft["url"] = session["app"].url
        return draft

    def start(self, project_id):
        self.ensure_local()
        with self.lock:
            project = self.store.project(project_id)
            profile = self.profile(project)
            if any(s["thread"].is_alive() for s in self.sessions.values()):
                raise Conflict("Stop the current Studio preview before starting another")
            artifact = json.loads(project["artifact"] or "null")
            if not artifact or project["state"] != "ready" or project.get("pending_edit"):
                raise Conflict("Build and verify this revision before opening a Studio draft")
            draft_id = uid()
            with self.store.tx() as db:
                self.idle(db, project_id)
                existing = db.execute("SELECT * FROM studio_drafts WHERE project_id=?", (project_id,)).fetchone()
                if existing and existing["state"] not in ("imported", "discarded"):
                    # Resume the same editable files; never silently discard user work.
                    if existing["base_job"] != artifact["job_id"] or existing["revision"] != project["revision"]:
                        raise Conflict("Draft is stale; inspect or discard it before creating another")
                    draft_id = existing["id"]
                    db.execute("UPDATE studio_drafts SET state='preparing' WHERE id=?", (draft_id,))
                else:
                    destination = self.settings.data / "studio" / draft_id / "source"
                    destination.parent.mkdir(parents=True)
                    clone_tree(self.settings.data / "jobs" / artifact["job_id"] / "source", destination)
                    db.execute("INSERT OR REPLACE INTO studio_drafts VALUES(?,?,?,?,?,?)",
                               (draft_id, project_id, artifact["job_id"], project["revision"], "preparing", time.time()))
                self.store.event_tx(db, project_id, None, "studio.preparing", {"draft": draft_id})
            session = {"cancel": threading.Event(), "app": None}
            session["thread"] = threading.Thread(target=self._serve, args=(project_id, draft_id, profile, session), daemon=True)
            self.sessions[project_id] = session
            session["thread"].start()
            return self.state(project_id)

    def _serve(self, project, draft_id, profile, session):
        def cancelled():
            return self.stop.is_set() or session["cancel"].is_set()
        source = self.settings.data / "studio" / draft_id / "source"
        state = "stopped"
        try:
            env = local_env()
            env.update({"NUXT_TELEMETRY_DISABLED": "1", "npm_config_cache": os.environ.get("FACTORY_NPM_CACHE", str(source.parent / "npm-cache"))})
            run(profile.studio_install_argv, cwd=source, env=env, timeout=profile.timeout, cancelled=cancelled)
            if cancelled():
                return
            app = LocalApp(profile.studio_dev_argv, source, env)
            session["app"] = app
            app.wait_ready(timeout=180, cancelled=cancelled)
            with self.store.tx() as db:
                db.execute("UPDATE studio_drafts SET state='editing' WHERE id=?", (draft_id,))
                self.store.event_tx(db, project, None, "studio.ready", {"draft": draft_id})
            deadline = time.monotonic() + 1800
            while not cancelled() and time.monotonic() < deadline and app.process.poll() is None:
                session["cancel"].wait(0.5)
        except Exception:
            state = "stopped" if cancelled() else "failed"
        finally:
            if session["app"]:
                session["app"].close()
            with self.store.tx() as db:
                db.execute("UPDATE studio_drafts SET state=? WHERE id=?", (state, draft_id))
                self.store.event_tx(db, project, None, "studio." + state, {"draft": draft_id})

    def stop_preview(self, project):
        with self.lock:
            session = self.sessions.get(project)
            if session:
                session["cancel"].set()
                session["thread"].join(timeout=15)
                if session["thread"].is_alive():
                    raise Conflict("Preview is stopping; retry shortly")
            return self.state(project)

    def close(self):
        self.stop.set()
        for project in list(self.sessions):
            self.stop_preview(project)

    def content(self, root):
        content = root / "content"
        if content.is_symlink():
            raise ValueError("Content directory may not be a symlink")
        if not content.exists():
            return {}
        result = {}
        for path in files_under(content, limit=200_000):
            if path.suffix not in (".md", ".json", ".yaml", ".yml"):
                raise ValueError("Studio import supports Markdown, JSON and YAML content only")
            result[path.relative_to(content).as_posix()] = path.read_text()
        return result

    def diff(self, project):
        draft = self.state(project)
        if draft["state"] == "none":
            return {"files": []}
        before = self.content(self.settings.data / "jobs" / draft["base_job"] / "source")
        after = self.content(self.settings.data / "studio" / draft["id"] / "source")
        return {"files": [{"path": "content/" + key, "before": before.get(key), "after": after.get(key)}
                           for key in sorted(before.keys() | after.keys()) if before.get(key) != after.get(key)]}

    def apply(self, project_id):
        self.ensure_local()
        with self.lock:
            self.stop_preview(project_id)
            draft = self.state(project_id)
            if draft["state"] not in ("stopped", "failed"):
                raise Conflict("No editable draft to apply")
            if not self.diff(project_id)["files"]:
                raise Conflict("No content changes to import")
            edit, job = uid(), uid()
            with self.store.tx() as db:
                self.idle(db, project_id)
                project = dict(db.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone())
                artifact = json.loads(project["artifact"] or "null")
                if project["revision"] != draft["revision"] or not artifact or artifact["job_id"] != draft["base_job"]:
                    raise Conflict("Project changed since draft creation; discard and reopen against the latest build")
                if Plan.model_validate_json(project["plan"]).questions:
                    raise Conflict("Resolve plan questions before verifying edits")
                destination = self.settings.data / "edits" / edit / "source"
                destination.parent.mkdir(parents=True)
                clone_tree(self.settings.data / "jobs" / draft["base_job"] / "source", destination)
                after = self.content(self.settings.data / "studio" / draft["id"] / "source")
                if (destination / "content").exists():
                    shutil.rmtree(destination / "content")
                for name, value in after.items():
                    path = destination / "content" / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(value)
                revision = project["revision"] + 1
                db.execute("UPDATE projects SET pending_edit=?,revision=?,state='building' WHERE id=?", (edit, revision, project_id))
                db.execute("INSERT INTO jobs(id,project_id,kind,payload,created) VALUES(?,?,'verify',?,?)",
                           (job, project_id, json.dumps({"revision": revision}), time.time()))
                db.execute("UPDATE studio_drafts SET state='imported' WHERE id=?", (draft["id"],))
                self.store.event_tx(db, project_id, job, "studio.applied", {"revision": revision, "draft": draft["id"]})
            return self.store.one("SELECT * FROM jobs WHERE id=?", (job,))

    def discard(self, project):
        with self.lock:
            self.stop_preview(project)
            with self.store.tx() as db:
                db.execute("UPDATE studio_drafts SET state='discarded' WHERE project_id=?", (project,))
                self.store.event_tx(db, project, None, "studio.discarded", {})
        return self.state(project)

    def starter(self, project_id):
        self.ensure_local()
        project = self.store.project(project_id)
        self.profile(project)
        edit, job = uid(), uid()
        template = Path(__file__).parent / "starters" / "nuxt-studio"
        plan = Plan.model_validate_json((template / "factory-plan.json").read_text())
        with self.store.tx() as db:
            self.idle(db, project_id)
            row = db.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
            if row["state"] != "draft" or row["plan"] or row["pending_edit"]:
                raise Conflict("Starter can only initialize a new empty project")
            destination = self.settings.data / "edits" / edit / "source"
            destination.mkdir(parents=True)
            for path in template.rglob("*"):
                if path.is_file() and path.name != "factory-plan.json":
                    target = destination / path.relative_to(template)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(path, target)
            db.execute("UPDATE projects SET pending_edit=?,revision=1,plan=?,state='building' WHERE id=?",
                       (edit, plan.model_dump_json(), project_id))
            db.execute("INSERT INTO jobs(id,project_id,kind,payload,created) VALUES(?,?,'verify',?,?)",
                       (job, project_id, json.dumps({"revision": 1}), time.time()))
            self.store.event_tx(db, project_id, job, "studio.starter_created", {})
        return self.store.one("SELECT * FROM jobs WHERE id=?", (job,))
