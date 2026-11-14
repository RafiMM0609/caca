"""Server-sent events for the dashboard.

**Why this is not `EventSource`.** The browser's `EventSource` cannot send an
`Authorization` header, and this API is bearer-token only with no cookie auth by design
(`docs/operations.md`). The only ways to make `EventSource` work are a cookie — which
reintroduces CSRF, a model the project deliberately does not have — or a token in the query
string, which leaks into access logs, proxy logs and browser history. Neither is acceptable,
so the client reads this stream with `fetch()` and `ReadableStream`, where the header is
preserved. This module only has to produce a correct `text/event-stream`.

**Why the server still reads SQLite every second.** The API and the scheduler are separate
processes (`api.py` never imports `Engine`), so there is no in-process event bus: nothing can
push to a waiting client. The stream polls the database on a short interval and forwards a
delta. That is still cheaper than the old browser poll by two orders of magnitude, because
the query is indexed and returns only rows after a cursor — measured at 2 bytes when idle.
SSE here buys *latency*, not resource savings; the cursor work bought the savings.

**Why the loop is bounded.** A long-lived stream must not outlive the job it is watching
forever, must notice a vanished client, and must not hold an async slot if it wedges. The
keepalive comment is what keeps proxies and browsers from closing an idle connection, and it
is also the point where a disconnect is observed.
"""
import asyncio
import json
import time

# Used only for the type hint on the fetch callable; the route supplies the real query.
EVENTS_SQL = "SELECT * FROM events WHERE project_id=? AND seq>? ORDER BY seq LIMIT ?"


# A stream that outlives this is re-established by the client, which reconnects with the same
# cursor, so a short lifetime costs one round trip and bounds how long a wedged stream lives.
MAX_STREAM_SECONDS = 600
POLL_INTERVAL = 1.0
# Proxies and browsers drop a silent connection; a comment line every 15s keeps it open.
KEEPALIVE_SECONDS = 15.0
MAX_BATCH = 300


def format_event(event):
    """One SSE frame. `id:` is the global seq, which is what makes reconnect resumable."""
    detail = event.get("detail") or "{}"
    payload = {
        "seq": event["seq"],
        "kind": event["kind"],
        "detail": detail,
        "created": event["created"],
        "job_id": event.get("job_id"),
    }
    return "id: {}\nevent: {}\ndata: {}\n\n".format(
        event["seq"], event["kind"], json.dumps(payload, separators=(",", ":")))


async def event_frames(fetch, project: str, after: int, *, clock=time.monotonic,
                       sleep=asyncio.sleep, poll_interval=POLL_INTERVAL,
                       keepalive=KEEPALIVE_SECONDS, max_seconds=MAX_STREAM_SECONDS):
    """Yield SSE frames for one project, starting after `after`.

    `fetch` is an async callable `(cursor) -> rows` rather than a Store, because the SQLite read
    is blocking: the route wraps it in a worker thread so a stream cannot stall the event loop
    that every other request shares. Injecting it also lets a test drive this with no server.
    Written as a generator so the transport lives in the route and this stays testable.
    """
    cursor = int(after)
    started = clock()
    last_write = started
    # An immediate comment flushes headers through proxies that wait for a first byte.
    yield ": connected\n\n"
    while True:
        if clock() - started > max_seconds:
            # Not an error: the client reconnects with its cursor and resumes exactly here.
            yield "event: reconnect\ndata: {}\n\n".format(json.dumps({"reason": "max_lifetime"}))
            return
        rows = await fetch(cursor)
        for row in rows:
            cursor = row["seq"]
            last_write = clock()
            yield format_event(row)
        if not rows and clock() - last_write >= keepalive:
            last_write = clock()
            yield ": keepalive\n\n"
        await sleep(poll_interval)
