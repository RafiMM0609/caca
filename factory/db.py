import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path


def uid():
    return uuid.uuid4().hex


class Conflict(ValueError):
    pass


# Which job kinds a lane claims. Written once because the two call sites drifted apart easily:
# a new kind added to one and not the other is claimed by the wrong lane, and the symptom is a
# long-lived app started inside the build lane that then exits.
LANE_KINDS = {
    "build": ("build", "plan", "verify"),
    "deploy": ("deploy",),
    "serve": ("serve",),
}


def lane_predicate(lane):
    kinds = LANE_KINDS.get(lane)
    if kinds is None:
        raise ValueError(f"Unknown lane: {lane}")
    return "kind IN (" + ",".join("'" + kind + "'" for kind in kinds) + ")"


class Store:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS schema_version(version INTEGER PRIMARY KEY);
                INSERT OR IGNORE INTO schema_version VALUES(1);
                CREATE TABLE IF NOT EXISTS projects(
                  id TEXT PRIMARY KEY, name TEXT NOT NULL, brief TEXT NOT NULL, profile TEXT NOT NULL,
                  state TEXT NOT NULL DEFAULT 'draft', revision INTEGER NOT NULL DEFAULT 0,
                  plan TEXT, artifact TEXT, created REAL NOT NULL,
                  client_id TEXT NOT NULL DEFAULT '');
                CREATE TABLE IF NOT EXISTS jobs(
                  id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), kind TEXT NOT NULL,
                  state TEXT NOT NULL DEFAULT 'queued', payload TEXT NOT NULL DEFAULT '{}',
                  result TEXT, error TEXT, created REAL NOT NULL, started REAL, finished REAL,
                  heartbeat REAL, cancel INTEGER NOT NULL DEFAULT 0, reserved_usd REAL NOT NULL DEFAULT 0);
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_project ON jobs(project_id)
                  WHERE state IN ('queued','running');
                CREATE TABLE IF NOT EXISTS events(
                  seq INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT, job_id TEXT,
                  kind TEXT NOT NULL, detail TEXT NOT NULL, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS deployments(
                  id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
                  job_id TEXT NOT NULL, environment TEXT NOT NULL, digest TEXT NOT NULL,
                  state TEXT NOT NULL, receipt TEXT, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS runtime(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS studio_drafts(
                  id TEXT PRIMARY KEY, project_id TEXT NOT NULL UNIQUE REFERENCES projects(id),
                  base_job TEXT NOT NULL, revision INTEGER NOT NULL, state TEXT NOT NULL, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS workers(
                  id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
                  job_id TEXT NOT NULL, task_id TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'queued',
                  branch TEXT NOT NULL, worktree TEXT, base_commit TEXT, commit_sha TEXT,
                  depends_on TEXT NOT NULL DEFAULT '[]', attempts INTEGER NOT NULL DEFAULT 0,
                  detail TEXT NOT NULL DEFAULT '{}', created REAL NOT NULL, started REAL, finished REAL,
                  revision INTEGER NOT NULL DEFAULT 0);
                CREATE INDEX IF NOT EXISTS workers_by_job ON workers(job_id, task_id);
                -- One row per project, because only one build of a project is served at a time.
                -- The port is allocated at start and released at stop; `pid` exists so a row
                -- left behind by a crash can be recognised as stale rather than believed.
                CREATE TABLE IF NOT EXISTS served_apps(
                  project_id TEXT PRIMARY KEY REFERENCES projects(id),
                  job_id TEXT NOT NULL, digest TEXT NOT NULL, port INTEGER NOT NULL,
                  state TEXT NOT NULL, pid INTEGER, restarts INTEGER NOT NULL DEFAULT 0,
                  detail TEXT NOT NULL DEFAULT '{}', started REAL NOT NULL, checked REAL);
            """)
            db.execute("BEGIN IMMEDIATE")
            try:
                columns = {row[1] for row in db.execute("PRAGMA table_info(projects)")}
                if "pending_edit" not in columns:
                    db.execute("ALTER TABLE projects ADD COLUMN pending_edit TEXT")
                if "client_id" not in columns:
                    db.execute("ALTER TABLE projects ADD COLUMN client_id TEXT NOT NULL DEFAULT ''")
                # Resume is only valid within the plan revision that produced the work: after
                # an edit the same task id means different code, so a merged row from an older
                # revision must not be treated as done.
                worker_columns = {row[1] for row in db.execute("PRAGMA table_info(workers)")}
                if "revision" not in worker_columns:
                    db.execute("ALTER TABLE workers ADD COLUMN revision INTEGER NOT NULL DEFAULT 0")
                db.commit()
            except BaseException:
                db.rollback()
                raise

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=10000")
        db.execute("PRAGMA synchronous=FULL")
        return db

    @contextmanager
    def tx(self):
        db = self.connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def all(self, sql, args=()):
        with self.connect() as db:
            return [dict(row) for row in db.execute(sql, args)]

    def one(self, sql, args=()):
        rows = self.all(sql, args)
        if not rows:
            raise KeyError("Record not found")
        return rows[0]

    @staticmethod
    def event_tx(db, project, job, kind, detail):
        db.execute("INSERT INTO events(project_id,job_id,kind,detail,created) VALUES(?,?,?,?,?)",
                   (project, job, kind, json.dumps(detail), time.time()))

    def event(self, project, job, kind, detail):
        with self.tx() as db:
            self.event_tx(db, project, job, kind, detail)

    def create_project(self, payload):
        project = uid()
        with self.tx() as db:
            db.execute("INSERT INTO projects(id,name,brief,profile,created,client_id) "
                       "VALUES(?,?,?,?,?,?)",
                       (project, payload.name, payload.brief, payload.profile, time.time(),
                        payload.client_id))
            self.event_tx(db, project, None, "project.created",
                          {"name": payload.name, "client_id": payload.client_id})
        return self.project(project)

    def project(self, project):
        return self.one("SELECT * FROM projects WHERE id=?", (project,))

    def client_projects(self, client_id):
        """Every project attributed to one client, oldest first.

        Client attribution is a label, not an authorization boundary (there is one operator
        and clients do not log in). Its operational use is bulk hand-off and retirement: to
        give one client everything that is theirs, or to find everything that must be
        removed when a relationship ends, without walking the project list by hand.
        """
        return self.all("SELECT * FROM projects WHERE client_id=? ORDER BY created", (client_id,))

    def enqueue(self, project, kind, payload=None, allowed=None, next_state=None):
        job = uid()
        try:
            with self.tx() as db:
                row = db.execute("SELECT * FROM projects WHERE id=?", (project,)).fetchone()
                if row is None:
                    raise KeyError("Project not found")
                if allowed and row["state"] not in allowed:
                    raise Conflict(f"Project is {row['state']}")
                if kind == "build":
                    if not payload or payload.get("revision") != row["revision"]:
                        raise Conflict("Plan revision changed; review it again")
                    if not row["plan"] or json.loads(row["plan"])["questions"]:
                        raise Conflict("Resolve planner questions before approval")
                    payload = {"revision": row["revision"]}
                if kind == "deploy":
                    artifact = json.loads(row["artifact"] or "null")
                    if not artifact or artifact["digest"] != payload["digest"]:
                        raise Conflict("Release digest differs from current artifact")
                    env = payload["environment"]
                    if env == "production" and not db.execute(
                        "SELECT 1 FROM deployments WHERE project_id=? AND environment='staging' "
                        "AND digest=? AND state='succeeded'", (project, payload["digest"])
                    ).fetchone():
                        raise Conflict("This exact artifact must pass staging first")
                    if db.execute("SELECT 1 FROM deployments WHERE project_id=? AND environment=? "
                                  "AND digest=? AND state='succeeded'", (project, env, payload["digest"])).fetchone():
                        raise Conflict("Artifact already deployed to this environment")
                    unresolved = db.execute("SELECT 1 FROM deployments WHERE project_id=? AND environment=? "
                                            "AND state IN ('running','unknown')", (project, env)).fetchone()
                    if unresolved:
                        raise Conflict("Reconcile the unresolved deployment before another release")
                db.execute("INSERT INTO jobs(id,project_id,kind,payload,created) VALUES(?,?,?,?,?)",
                           (job, project, kind, json.dumps(payload or {}), time.time()))
                if next_state:
                    db.execute("UPDATE projects SET state=? WHERE id=?", (next_state, project))
                self.event_tx(db, project, job, f"{kind}.queued", payload or {})
        except sqlite3.IntegrityError as exc:
            raise Conflict("Project already has an active job") from exc
        return self.one("SELECT * FROM jobs WHERE id=?", (job,))

    def claim(self, lane="build"):
        with self.tx() as db:
            predicate = lane_predicate(lane)
            row = db.execute("SELECT * FROM jobs WHERE state='queued' AND " + predicate + " ORDER BY created,id LIMIT 1").fetchone()
            if not row:
                return None
            now = time.time()
            db.execute("UPDATE jobs SET state='running',started=?,heartbeat=? WHERE id=?", (now, now, row["id"]))
            self.event_tx(db, row["project_id"], row["id"], "job.started", {})
            return dict(row)

    def finish(self, job, state, result=None, error=None):
        with self.tx() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job,)).fetchone()
            db.execute("UPDATE jobs SET state=?,result=?,error=?,finished=? WHERE id=?",
                       (state, json.dumps(result), error, time.time(), job))
            if row["kind"] != "deploy" and state != "succeeded":
                db.execute("UPDATE projects SET state=? WHERE id=?", (state, row["project_id"]))
            self.event_tx(db, row["project_id"], job, f"job.{state}", {"error": error})

    # -- served apps --------------------------------------------------------------------

    def served(self, project):
        """The served-app row, or None. `one()` raises on a miss, and 'not served' is normal."""
        rows = self.all("SELECT * FROM served_apps WHERE project_id=?", (project,))
        return rows[0] if rows else None

    def served_apps(self):
        return self.all("SELECT * FROM served_apps ORDER BY started DESC")

    def record_served(self, project, job, digest, port, state, detail=None):
        with self.tx() as db:
            db.execute(
                "INSERT INTO served_apps(project_id,job_id,digest,port,state,detail,"
                "started,checked) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(project_id) DO UPDATE SET "
                "job_id=excluded.job_id, digest=excluded.digest, port=excluded.port, "
                "state=excluded.state, started=excluded.started, checked=excluded.checked, "
                "detail=excluded.detail",
                (project, job, digest, port, state, json.dumps(detail or {}),
                 time.time(), time.time()))

    def set_served_state(self, project, state, *, pid=None, detail=None, bump_restart=False):
        with self.tx() as db:
            db.execute("UPDATE served_apps SET state=?, checked=?, "
                       "pid=COALESCE(?, pid), "
                       "detail=COALESCE(?, detail), "
                       "restarts=restarts+? WHERE project_id=?",
                       (state, time.time(), pid,
                        json.dumps(detail) if detail is not None else None,
                        1 if bump_restart else 0, project))

    def forget_served(self, project):
        with self.tx() as db:
            db.execute("DELETE FROM served_apps WHERE project_id=?", (project,))

    def recover(self, lane="build"):
        # Called only while holding the single scheduler's flock. Never replay a side effect.
        with self.tx() as db:
            predicate = lane_predicate(lane)
            for row in db.execute("SELECT * FROM jobs WHERE state='running' AND " + predicate).fetchall():
                db.execute("UPDATE jobs SET state='interrupted',error=?,finished=? WHERE id=?",
                           ("Scheduler restarted; inspect evidence before retry", time.time(), row["id"]))
                if row["kind"] != "deploy":
                    db.execute("UPDATE projects SET state='interrupted' WHERE id=?", (row["project_id"],))
                # A worker left 'queued' or 'running' by a dead process would otherwise stay
                # 'running' forever, and the dashboard would show work in progress for a job
                # that already ended. Workers that already reached a terminal state keep it.
                db.execute("UPDATE workers SET state='interrupted', finished=? "
                           "WHERE job_id=? AND state IN ('queued','running')",
                           (time.time(), row["id"]))
                db.execute("UPDATE deployments SET state='unknown' WHERE job_id=? AND state='running'", (row["id"],))
                self.event_tx(db, row["project_id"], row["id"], "job.interrupted", {})
            # Reconcile workers for every finished job, not only jobs interrupted just now.
            # A worker row can be left 'running' because the process died after the job was
            # marked terminal, and an older database keeps that lie forever. Workers in a
            # job that is still running are left alone.
            db.execute(
                "UPDATE workers SET state='interrupted', finished=? "
                "WHERE state IN ('queued','running') AND job_id IN "
                "(SELECT id FROM jobs WHERE state NOT IN ('queued','running'))",
                (time.time(),))

    def cancel(self, job):
        with self.tx() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job,)).fetchone()
            if not row:
                raise KeyError("Job not found")
            if row["state"] not in ("queued", "running"):
                raise Conflict("Job is already terminal")
            if row["state"] == "queued":
                db.execute("UPDATE jobs SET state='cancelled',cancel=1,finished=? WHERE id=?", (time.time(), job))
                if row["kind"] != "deploy":
                    db.execute("UPDATE projects SET state='cancelled' WHERE id=?", (row["project_id"],))
            else:
                db.execute("UPDATE jobs SET cancel=1 WHERE id=?", (job,))
            self.event_tx(db, row["project_id"], job, "job.cancel_requested", {})

    def heartbeat(self, job=None, lane="build"):
        with self.tx() as db:
            now = time.time()
            db.execute("INSERT OR REPLACE INTO runtime VALUES(?,?)", (lane + "_heartbeat", str(now)))
            if job:
                db.execute("UPDATE jobs SET heartbeat=? WHERE id=? AND state='running'", (now, job))

    # --- workers ---------------------------------------------------------------------
    # A worker is one Pi harness invocation for one plan task in its own worktree.
    # These rows are what makes "the planner assigned work to workers" observable.

    def create_worker(self, project, job, task_id, branch, base_commit, depends_on=(),
                      worktree=None, revision=0):
        worker_id = uid()
        with self.tx() as db:
            db.execute("INSERT INTO workers(id,project_id,job_id,task_id,branch,worktree,base_commit,"
                       "depends_on,created,revision) VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (worker_id, project, job, task_id, branch, str(worktree) if worktree else None,
                        base_commit, json.dumps(list(depends_on)), time.time(), revision))
            self.event_tx(db, project, job, "worker.created",
                          {"worker": worker_id, "task": task_id, "branch": branch})
        return self.one("SELECT * FROM workers WHERE id=?", (worker_id,))

    def worker(self, worker_id):
        return self.one("SELECT * FROM workers WHERE id=?", (worker_id,))

    def workers_for(self, job):
        return self.all("SELECT * FROM workers WHERE job_id=? ORDER BY created", (job,))

    def set_worker(self, worker_id, **fields):
        if not fields:
            return
        allowed = {"state", "worktree", "commit_sha", "attempts", "detail", "started", "finished"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"Unknown worker fields: {', '.join(sorted(unknown))}")
        payload = dict(fields)
        if "detail" in payload and not isinstance(payload["detail"], str):
            payload["detail"] = json.dumps(payload["detail"])
        # Paths arrive as Path objects; sqlite only binds str/int/float/bytes/None.
        for name in ("worktree", "commit_sha", "state"):
            if name in payload and payload[name] is not None:
                payload[name] = str(payload[name])
        assignments = ", ".join(f"{name}=?" for name in payload)
        with self.tx() as db:
            db.execute(f"UPDATE workers SET {assignments} WHERE id=?",
                       (*payload.values(), worker_id))

    def worker_event(self, project, job, kind, detail):
        self.event(project, job, kind, detail)

    def worker_states(self, job):
        rows = self.all("SELECT task_id, state FROM workers WHERE job_id=?", (job,))
        return {row["task_id"]: row["state"] for row in rows}

    def completed_tasks(self, job):
        rows = self.all("SELECT task_id FROM workers WHERE job_id=? AND state='merged'", (job,))
        return {row["task_id"] for row in rows}

    def tasks_merged_for_project(self, project, revision, present_commits):
        """Tasks whose merged commit is still reachable on the product branch.

        A retry creates a **new** job id, so `completed_tasks(job_id)` finds nothing after a
        restart: the work is real, it is just filed under the previous job. Worse, believing
        an old row is enough would be unsafe — the branch may have been reset or the commit
        may never have landed. So both facts are required: a worker row recorded as merged for
        this plan revision, *and* that commit still reachable in the repository now.

        ``present_commits`` is the set of commits on the current branch; the caller reads it
        from git so this method never guesses about repository state.
        """
        # Deliberately no join on `jobs`: the worker row already carries the project, and
        # requiring a live job row would make resume depend on job history surviving rather
        # than on the work itself. `commit_sha` is the fact that matters, and it is checked
        # against the branch below.
        rows = self.all(
            "SELECT task_id, commit_sha FROM workers "
            "WHERE project_id=? AND revision=? AND state='merged' AND commit_sha IS NOT NULL "
            "ORDER BY finished",
            (project, revision))
        reachable = set(present_commits)
        done = set()
        for row in rows:
            if row["commit_sha"] in reachable:
                done.add(row["task_id"])
        return done

    def backup(self, destination):
        with self.connect() as source, sqlite3.connect(destination) as dest:
            source.backup(dest)
            if dest.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("Backup integrity check failed")
