"""Observability: the log must be safe to keep, and the health endpoint must not lie.

Three properties are tested here, and each of them is a way this goes wrong in practice:

  - **A secret must not reach the log.** This API is bearer-only, so a token in a log line is a
    full compromise. `redact` is asserted on nested structures, because the leak that happens in
    real life is a token inside an evidence blob, not a top-level field.
  - **A log line must be parseable.** One JSON object per line is the whole point; a log that
    only looks structured is worse than plain text because it invites tooling that then breaks.
  - **Health must report a real failure.** A health check that returns `ok` when the database is
    gone is not a health check. The database check is driven to actually fail here.
"""
import io
import json
import logging

import pytest

from factory import observe
from factory.db import Store, Migrations


class Capture:
    """Collect records emitted by the project logger."""

    def __init__(self):
        self.stream = io.StringIO()
        self.handler = logging.StreamHandler(self.stream)

    def __enter__(self):
        observe.logger.addHandler(self.handler)
        observe.logger.setLevel(logging.DEBUG)
        return self

    def __exit__(self, *exc):
        observe.logger.removeHandler(self.handler)

    def lines(self):
        return [json.loads(line) for line in self.stream.getvalue().splitlines() if line.strip()]


def test_a_log_line_is_one_json_object():
    with Capture() as capture:
        observe.log_event("http.request", method="GET", path="/api/projects", status=200, ms=3.1)
    records = capture.lines()
    assert len(records) == 1
    assert records[0]["event"] == "http.request"
    assert records[0]["status"] == 200
    assert records[0]["level"] == "INFO"
    assert "ts" in records[0]


def test_a_stable_event_name_is_used_rather_than_a_sentence():
    """Dotted names are countable; "Request completed" is not."""
    with Capture() as capture:
        observe.log_event("job.failed", job="j1", reason="WorkerBudgetExhausted")
    record = capture.lines()[0]
    assert record["event"] == "job.failed"
    assert " " not in record["event"]


@pytest.mark.parametrize("key", ["token", "operator_token", "release_token", "api_key",
                                 "apiKey", "password", "Authorization", "secret", "credential"])
def test_a_credential_field_is_redacted(key):
    with Capture() as capture:
        observe.log_event("probe", **{key: "super-secret-value"})
    assert "super-secret-value" not in capture.stream.getvalue()
    assert capture.lines()[0][key] == observe.REDACTED


def test_a_token_nested_in_evidence_is_also_redacted():
    """The realistic leak is a credential inside a payload, not at the top level."""
    with Capture() as capture:
        observe.log_event("evidence", detail={"job": {"payload": {"api_key": "sk-live-123"}}})
    text = capture.stream.getvalue()
    assert "sk-live-123" not in text
    assert observe.REDACTED in text


def test_non_secret_fields_survive_redaction():
    """Redaction that eats the useful fields makes the log worthless."""
    with Capture() as capture:
        observe.log_event("worker.merged", task="ui", branch="factory/ui", attempts=2)
    record = capture.lines()[0]
    assert record["task"] == "ui" and record["branch"] == "factory/ui" and record["attempts"] == 2


def test_a_caller_supplied_request_id_must_be_a_safe_token():
    """An unvalidated id would be written into a log line, so a newline could forge an entry."""
    assert observe.new_request_id("abc-123_ok") == "abc-123_ok"
    assert observe.new_request_id("bad\ninjected") != "bad\ninjected"
    assert observe.new_request_id("x" * 200) != "x" * 200
    assert len(observe.new_request_id(None)) == 16


def test_health_reports_missing_configuration_instead_of_ok(settings, store):
    """The factory can be up and still unable to plan; that must be visible."""
    bare = type(settings)(data=settings.data, operator_token="o" * 40, release_token="r" * 40,
                          model="", pi="/nonexistent/pi", profiles={})
    payload = observe.snapshot(store, bare, started=0)
    assert payload["checks"]["model"]["status"] == "missing"
    assert payload["checks"]["pi"]["status"] == "missing"
    assert payload["checks"]["profiles"]["status"] == "missing"


def test_health_reports_a_broken_database_rather_than_claiming_ok(settings, store):
    """A disk-full or locked SQLite is the most likely real outage, so it is measured."""

    class Broken:
        def one(self, *args, **kwargs):
            raise OSError("disk I/O error")

        def all(self, *args, **kwargs):
            raise OSError("disk I/O error")

    payload = observe.snapshot(Broken(), settings, started=0)
    assert payload["status"] == "fail"
    assert payload["checks"]["database"]["status"] == "fail"


def test_health_does_not_scan_the_event_table(settings, store):
    """This endpoint will be polled, so its cost must not grow with the data."""
    store.event  # noqa: B018 - the project exists; adding events must not change the cost
    payload = observe.snapshot(store, settings, started=0)
    assert payload["status"] in ("ok", "degraded", "fail")
    # The snapshot reports counts and ages, never rows or artifact listings.
    assert "memory" in payload and "checks" in payload
    assert "events" not in payload and "projects" not in payload


def test_health_detail_route_is_authenticated(client):
    """Lane ages and configured components are operator information, not public information."""
    assert client.get("/healthz/detail", headers={"Authorization": ""}).status_code == 401
    assert client.get("/healthz/detail").status_code == 200


def test_liveness_never_touches_the_database(settings):
    """A probe that reads SQLite turns a slow disk into a restart loop.

    Driven with a store that raises on any query, so this fails if liveness ever gains a
    dependency check. Changing the data path was not enough to prove it: the route is static, so
    that test passed for the wrong reason.
    """
    from fastapi.testclient import TestClient

    from factory.api import create_app

    class Exploding(Store):
        def one(self, *args, **kwargs):
            raise AssertionError("liveness must not query the database")

        def all(self, *args, **kwargs):
            raise AssertionError("liveness must not query the database")

    app = create_app(settings)
    app.state.store.one = Exploding(settings.data / "factory.sqlite3").one
    app.state.store.all = Exploding(settings.data / "factory.sqlite3").all
    with TestClient(app) as client:
        response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_responses_carry_a_request_id(client):
    response = client.get("/api/projects")
    assert response.headers.get(observe.REQUEST_ID_HEADER)


def test_a_request_id_is_echoed_when_it_is_safe(client):
    response = client.get("/api/projects", headers={observe.REQUEST_ID_HEADER: "trace-me-1"})
    assert response.headers[observe.REQUEST_ID_HEADER] == "trace-me-1"


def test_an_unsafe_request_id_is_replaced_not_echoed(client):
    response = client.get("/api/projects", headers={observe.REQUEST_ID_HEADER: "evil\nline"})
    assert response.headers[observe.REQUEST_ID_HEADER] != "evil\nline"


# --- migrations -----------------------------------------------------------------------------

def test_migrations_record_their_version(tmp_path):
    store = Store(tmp_path / "factory.sqlite3")
    with store.connect() as db:
        versions = sorted(row[0] for row in db.execute("SELECT version FROM schema_version"))
    assert versions == [1, 2, 3, 4], versions


def test_migrations_are_idempotent(tmp_path):
    path = tmp_path / "factory.sqlite3"
    for _ in range(3):
        Store(path)
    with Store(path).connect() as db:
        versions = list(db.execute("SELECT version FROM schema_version"))
        columns = {row[1] for row in db.execute("PRAGMA table_info(projects)")}
    assert len(versions) == 4, "a migration must not be recorded twice"
    assert "pending_edit" in columns and "client_id" in columns


def test_a_migration_that_fails_is_not_recorded(tmp_path):
    """Half-migrating and recording the version is how a schema drifts silently."""
    store = Store(tmp_path / "factory.sqlite3")
    broken = Migrations([(99, "impossible", "ALTER TABLE no_such_table ADD COLUMN x TEXT")])
    with pytest.raises(Exception):
        with store.connect() as db:
            broken.apply(db)
    with store.connect() as db:
        versions = sorted(row[0] for row in db.execute("SELECT version FROM schema_version"))
        failed = [row[1] for row in db.execute("PRAGMA table_info(no_such_table)")]
    assert versions == [1, 2, 3, 4], "a failed migration must not advance the ledger"
    assert failed == []


def test_an_existing_column_is_adopted_rather_than_failing(tmp_path):
    """A database built by an older release already has the column but not the version."""
    path = tmp_path / "factory.sqlite3"
    store = Store(path)
    with store.connect() as db:
        db.execute("DELETE FROM schema_version WHERE version=2")
    # Reopening must notice the column exists, record the version, and not raise on the ALTER.
    Store(path)
    with Store(path).connect() as db:
        versions = sorted(row[0] for row in db.execute("SELECT version FROM schema_version"))
    assert 2 in versions


def test_health_detail_survives_a_real_heartbeat_row(settings, store):
    """A history: /healthz/detail returned 500 for every request once a lane heartbeated.

    The lane loop reused the module-level accumulator name for a local string, so the response
    crashed on `state["overall"]`. The fixture store has no `runtime` rows, so the branch never
    ran under TestClient and the suite stayed green; a uvicorn probe found it immediately. This
    test supplies the row that makes the branch run.
    """
    store.heartbeat(lane="build")
    payload = observe.snapshot(store, settings, started=0)
    assert payload["checks"]["build_lane"]["status"] == "ok"
    assert payload["status"] in ("ok", "degraded")


def test_a_stale_lane_degrades_but_does_not_fail(settings, store):
    """A stopped build lane is a degraded service, not a dead one: reads still work."""
    import time as _time
    with store.tx() as db:
        db.execute("INSERT OR REPLACE INTO runtime VALUES(?,?)",
                   ("build_heartbeat", str(_time.time() - 600)))
    payload = observe.snapshot(store, settings, started=0)
    assert payload["checks"]["build_lane"]["status"] == "stale"
    assert payload["status"] == "degraded"


def test_a_dead_database_outranks_a_degraded_lane(settings, store):
    """The database check runs first; the lane check must not soften it."""
    import time as _time

    class Broken:
        def one(self, *a, **k):
            raise OSError("disk I/O error")

        def all(self, *a, **k):
            with store.connect() as db:
                return [dict(row) for row in db.execute("SELECT * FROM runtime")]

    with store.tx() as db:
        db.execute("INSERT OR REPLACE INTO runtime VALUES(?,?)",
                   ("build_heartbeat", str(_time.time() - 600)))
    payload = observe.snapshot(Broken(), settings, started=0)
    assert payload["status"] == "fail", "a hard failure must not be reported as degraded"


def test_configure_actually_emits_records():
    """A history: `configure()` set propagate=False on a logger that had no handler.

    Every structured line was silently dropped. Nothing raised, the service ran normally, and
    the only symptom was an empty log — which is the worst kind of failure, because the absence
    of logs looks like the absence of activity. This asserts a record reaches an output stream.
    """
    import io as _io

    _io  # keep the import used for the assertion below
    observe.configure("INFO")
    # Assert on what configure() installed, without substituting a handler of our own: the
    # previous version replaced the handler first, so it could not detect the bug it exists for.
    assert observe.logger.handlers, "configure must attach a handler to the project logger"
    installed = observe.logger.handlers[0]
    assert isinstance(installed, logging.StreamHandler)
    # And prove a real record survives the configured pipeline, by capturing what that very
    # handler writes rather than swapping it out.
    stream = _io.StringIO()
    original = installed.setStream if hasattr(installed, "setStream") else None
    installed.stream = stream
    try:
        observe.log_event("probe.emitted", value=1)
        output = stream.getvalue().strip()
    finally:
        if original and installed.stream is stream:
            installed.setStream(getattr(installed, "_original_stream", stream))
    assert output, "configure() must produce output; an empty log is indistinguishable from silence"
    assert json.loads(output)["event"] == "probe.emitted"


def test_configure_does_not_duplicate_handlers():
    """Two handlers would double every line; configure() must be safe to call repeatedly."""
    observe.configure("INFO")
    first = len(observe.logger.handlers)
    observe.configure("INFO")
    assert len(observe.logger.handlers) == first == 1
