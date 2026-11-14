"""Structured logs, request correlation and a cheap health snapshot.

**Why a hand-rolled module instead of a library.** The whole API is one process on a 2 GB host.
`structlog` or `loguru` would work, but this needs four things and nothing else, and every
dependency in `pyproject.toml` is something an operator has to keep patched. The stdlib
`logging` module already does the transport and the level filtering.

**Why JSON lines.** A log that a human squints at is a log nobody queries. One object per line
means `jq`, `journalctl -o json` and any future shipping agent all work without a parser. The
format is deliberately flat: no nested `req` object, because flat keys are what `jq` filters on
and what log aggregators index.

**What is never logged.** The operator token, the release token, provider API keys, request
bodies, and Authorization headers. This is not a policy statement — `redact` is applied to every
value the logger sees, and the tests assert that a token cannot reach the output. A log file is
the easiest place in a system to leak a credential by accident, and this API is bearer-only, so a
leaked token is a full compromise.

**The one thing that must not be logged per iteration.** The event stream reads SQLite once a
second for the life of a connection. Logging each read would emit 3600 lines per hour per open
tab for no diagnostic value, so the stream route logs its start and end and nothing in between.
At ~195 bytes per line, an hour of per-read logging is only 0.8 MB, which sounds harmless until
it is running in a loop for weeks against a 14 GB disk. The rule is in the route's docstring too,
because the person who adds logging later will be reading the route, not this module.
"""
import json
import logging
import os
import re
import time
import uuid

# A request id is opaque and short. It is echoed to the client so a user-reported failure can be
# found in the log without asking them to reproduce it.
REQUEST_ID_HEADER = "X-Request-Id"
REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

# Anything whose key looks like a credential is replaced before it reaches the log. Matching on
# the key name rather than the value is deliberate: a token has no distinguishing shape at
# runtime, but the field holding it is always called something like `token` or `api_key`.
SECRET_KEY = re.compile(r"(token|secret|password|api[_-]?key|authorization|credential)", re.I)
REDACTED = "[redacted]"

logger = logging.getLogger("factory")


def redact(value, _depth=0):
    """Return `value` with every credential-looking field replaced.

    Recurses, because the interesting leak is not a top-level `token` field — it is a token
    nested inside evidence, a job payload or an error detail.
    """
    if _depth > 6:
        return "[depth-limit]"
    if isinstance(value, dict):
        return {key: (REDACTED if SECRET_KEY.search(str(key)) else redact(item, _depth + 1))
                for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(item, _depth + 1) for item in value]
    return value


def new_request_id(candidate=None):
    """Reuse a caller-supplied id only when it is a safe token, otherwise mint one.

    A caller-controlled id goes straight into a log line, so an unvalidated value could inject a
    newline and forge a log entry. Validating the shape is cheaper than escaping every line.
    """
    if candidate and REQUEST_ID.fullmatch(candidate):
        return candidate
    return uuid.uuid4().hex[:16]


def log_event(event, level=logging.INFO, **fields):
    """One structured line. `event` is a stable dotted name, not an English sentence.

    Stable names are what make logs countable and alertable: `http.request` can be averaged,
    whereas "Request completed in 3ms" cannot.
    """
    payload = {"ts": round(time.time(), 3), "level": logging.getLevelName(level),
               "event": event}
    payload.update({key: value for key, value in fields.items() if value is not None})
    logger.log(level, json.dumps(redact(payload), separators=(",", ":"), default=str))


def configure(level="INFO"):
    """Send JSON lines to stdout. Idempotent, so a test may call it repeatedly.

    The handler is replaced rather than added to, because two handlers would double every line
    and that has already been the cause of a doubled-log incident in most projects that try this.

    **The handler must be attached to this logger, not only to the root logger.** The first
    version called `basicConfig` and set `propagate = False`, which is silently wrong: with
    propagation off, records written to `factory` never reach the root handler, and the `factory`
    logger had none of its own — so every structured line was dropped. Nothing failed, no warning
    appeared, and the only symptom was an empty log while the service ran normally. `basicConfig`
    stays so libraries that use the root logger still emit, but our own handler is the one that
    matters.
    """
    numeric = getattr(logging, str(level).upper(), logging.INFO)
    logging.basicConfig(level=numeric, format="%(message)s", force=True)
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.handlers = [handler]
    logger.setLevel(numeric)
    logger.propagate = False
    return logger


def _proc_rss_mb():
    """Resident memory from /proc. Returns None when /proc is unavailable.

    Deliberately not `psutil`: this is the only metric that needs it, and adding a compiled
    dependency to a 2 GB single-service host to read one number is a poor trade.
    """
    try:
        with open("/proc/self/status") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    return round(int(line.split()[1]) / 1024, 1)
    except (OSError, ValueError, IndexError):
        return None
    return None


def _proc_threads():
    try:
        with open("/proc/self/status") as handle:
            for line in handle:
                if line.startswith("Threads:"):
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        return None
    return None


def snapshot(store, settings, started=None):
    """What an operator or a monitor needs to answer "is this healthy, and why not".

    Every field is cheap: one indexed query, one or two `/proc` reads, no subprocess. This is a
    health endpoint that will be polled, so it must never do work proportional to the number of
    projects, artifacts or events.

    `started` is passed in by the app so uptime is measured from process start rather than from
    the first request.
    """
    checks = {}
    # A severity ladder rather than assignment: the first version set `overall` directly in each
    # block, so a database failure reported as "fail" was downgraded to "degraded" by the lane
    # check that runs afterwards. Health that softens a hard failure is worse than no health
    # check, because it turns an outage into a warning.
    _rank = {"ok": 0, "degraded": 1, "fail": 2}
    state = {"overall": "ok"}

    def raise_to(level):
        if _rank[level] > _rank[state["overall"]]:
            state["overall"] = level

    # Database reachability. A disk-full or locked SQLite is the most likely real outage here,
    # so it is measured rather than assumed.
    try:
        began = time.perf_counter()
        store.one("SELECT 1 AS ok")
        checks["database"] = {"status": "ok", "ms": round((time.perf_counter() - began) * 1000, 2)}
    except Exception as exc:  # noqa: BLE001 - the point is to report any failure, not classify it
        checks["database"] = {"status": "fail", "error": type(exc).__name__}
        raise_to("fail")

    # Scheduler liveness. The build lane is what turns an approved plan into an artifact; if it
    # is not heartbeating, every job queues forever and the dashboard looks merely quiet.
    try:
        rows = {row["key"]: row["value"] for row in store.all("SELECT * FROM runtime")}
        stale = []
        for lane in ("build", "deploy", "serve"):
            value = rows.get(f"{lane}_heartbeat")
            if value is None:
                continue
            age = time.time() - float(value)
            # Named distinctly: this used to be called `state`, which shadowed the accumulator
            # above and turned every /healthz/detail response into a 500 ("string indices must
            # be integers"). TestClient did not catch it because the fixture's store has no
            # heartbeat rows, so this branch never ran there — the uvicorn probe did.
            lane_state = "ok" if age < 90 else "stale"
            checks[f"{lane}_lane"] = {"status": lane_state, "age_seconds": round(age, 1)}
            if lane_state != "ok":
                stale.append(lane)
        # Only the build lane degrades the whole service: deploy and serve are optional lanes and
        # a stopped deploy scheduler is a configuration choice, not an outage.
        if "build" in stale:
            raise_to("degraded")
    except Exception as exc:  # noqa: BLE001
        checks["lanes"] = {"status": "fail", "error": type(exc).__name__}
        raise_to("degraded")

    # Configuration that the workflow needs. Reported, never asserted: the factory can be up and
    # still unable to plan, which is exactly what an operator needs to be told.
    checks["model"] = {"status": "ok" if settings.model else "missing"}
    checks["pi"] = {"status": "ok" if _pi_available(settings) else "missing"}
    checks["profiles"] = {"status": "ok" if settings.profiles else "missing",
                          "count": len(settings.profiles)}

    memory = _proc_rss_mb()
    payload = {
        "status": state["overall"],
        "version": "0.1.0",
        "mode": "single-operator",
        "uptime_seconds": round(time.time() - started, 1) if started else None,
        "pid": os.getpid(),
        "memory": {"rss_mb": memory, "threads": _proc_threads()},
        "checks": checks,
    }
    return payload


def _pi_available(settings):
    import shutil
    return bool(shutil.which(settings.pi))
