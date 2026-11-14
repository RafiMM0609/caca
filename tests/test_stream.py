"""The event stream: framing, resumption and the two ways it must stop.

The stream is the one part of the dashboard that holds a connection open, so its failure modes
are different from an ordinary request: it can wedge forever, it can be buffered by a proxy, and
it can outlive the job it watches. These tests drive the generator directly, with a fake clock
and a fake fetch, so none of them needs a browser or a socket.

What is deliberately NOT tested here: that a real browser renders the events. That needs
Playwright and is a separate concern; the client-side parser is covered under `node --test`.
"""
import asyncio
import json
from pathlib import Path

from factory.db import Store
from factory.models import ProjectInput
from factory.stream import (MAX_BATCH, MAX_STREAM_SECONDS, POLL_INTERVAL, event_frames,
                            format_event)


def run(coro_fn):
    """Run one async test without adding an async pytest plugin.

    The project's dev dependencies are deliberately small, and driving an async generator only
    needs `asyncio.run`. The test body stays a plain coroutine so it reads normally.
    """
    import functools

    @functools.wraps(coro_fn)
    def wrapper(*args, **kwargs):
        return asyncio.run(coro_fn(*args, **kwargs))
    return wrapper


async def first(gen):
    return await gen.__anext__()


async def take(gen, n):
    """Take up to n frames. `sleep` is fake in every test below, so this cannot hang."""
    out = []
    for _ in range(n):
        try:
            out.append(await gen.__anext__())
        except StopAsyncIteration:
            break
    return out


async def drain(gen):
    out = []
    while True:
        try:
            out.append(await gen.__anext__())
        except StopAsyncIteration:
            return out


class Clock:
    """A monotonic clock that only moves when the code under test sleeps."""

    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    async def sleep(self, seconds):
        self.now += seconds


def make_project(store):
    project = store.create_project(ProjectInput(
        name="Stream test", brief="A project used to test the event stream end to end."))
    return project["id"]


def fetcher(store, project):
    async def fetch(cursor):
        return store.all(
            "SELECT * FROM events WHERE project_id=? AND seq>? ORDER BY seq LIMIT ?",
            (project, cursor, MAX_BATCH))
    return fetch


def test_a_frame_carries_the_global_seq_as_its_id():
    """`id:` is what makes a reconnect resumable, so it must be the seq, not an index."""
    frame = format_event({"seq": 42, "kind": "worker.merged", "detail": "{}",
                          "created": 1.0, "job_id": "j1"})
    assert frame.startswith("id: 42\n")
    assert "event: worker.merged\n" in frame
    assert frame.endswith("\n\n"), "a frame must be terminated by a blank line"
    payload = json.loads(frame.split("data: ", 1)[1])
    assert payload["seq"] == 42 and payload["job_id"] == "j1"


@run
async def test_the_first_frame_is_a_comment_so_headers_flush():
    """A proxy that waits for a first byte would otherwise hold the response indefinitely."""
    clock = Clock()
    gen = event_frames(lambda cursor: asyncio.sleep(0, []), "p", 0,
                       clock=clock, sleep=clock.sleep)
    assert await first(gen) == ": connected\n\n"


@run
async def test_history_after_the_cursor_is_replayed_then_the_stream_goes_quiet(tmp_path):
    """A fresh page must receive the whole history; a live one must receive only new events."""
    store = Store(tmp_path / "factory.sqlite3")
    project = make_project(store)
    store.event(project, None, "first", {})
    store.event(project, None, "second", {})
    cursor = store.all("SELECT max(seq) AS s FROM events WHERE project_id=?", (project,))[0]["s"]

    clock = Clock()
    gen = event_frames(fetcher(store, project), project, 0, clock=clock, sleep=clock.sleep)
    replayed = await take(gen, 12)
    kinds = [f.split("event: ")[1].split("\n")[0] for f in replayed if "event: " in f]
    # `project.created` is a real event in this project's history, so a full replay includes it.
    assert kinds == ["project.created", "first", "second"], kinds

    # Everything already delivered: a new stream from the cursor must not repeat it.
    quiet = event_frames(fetcher(store, project), project, cursor, clock=clock, sleep=clock.sleep)
    await first(quiet)
    seen = []
    for _ in range(3):
        seen.append(await first(quiet))
        clock.now += POLL_INTERVAL
    assert all(f.startswith(":") for f in seen), f"expected only comments, got {seen}"


@run
async def test_a_keepalive_is_emitted_while_idle():
    """An idle stream is normal; silence is what proxies and browsers time out."""
    clock = Clock()
    cursor = 10 ** 9  # past every real event, so the stream is genuinely idle
    gen = event_frames(lambda c: asyncio.sleep(0, []), "p", cursor,
                       clock=clock, sleep=clock.sleep, keepalive=5.0)
    await first(gen)
    lines = await take(gen, 8)
    assert ": keepalive\n\n" in lines, lines


@run
async def test_the_stream_ends_at_its_lifetime_and_asks_to_reconnect():
    """A wedged stream must not live forever; the client resumes from its cursor."""
    clock = Clock()
    gen = event_frames(lambda c: asyncio.sleep(0, []), "p", 0, clock=clock, sleep=clock.sleep,
                       max_seconds=MAX_STREAM_SECONDS)
    await first(gen)
    out = await drain(gen)
    assert any("event: reconnect" in f for f in out), "the stream must announce why it ended"
    assert clock.now >= MAX_STREAM_SECONDS
    assert json.loads(out[-1].split("data: ", 1)[1])["reason"] == "max_lifetime"


@run
async def test_a_new_event_written_during_a_stream_is_delivered(tmp_path):
    """This is the whole point: an event created after the page loaded must arrive."""
    store = Store(tmp_path / "factory.sqlite3")
    project = make_project(store)
    cursor = store.all("SELECT max(seq) AS s FROM events WHERE project_id=?", (project,))[0]["s"] or 0

    clock = Clock()
    gen = event_frames(fetcher(store, project), project, cursor, clock=clock, sleep=clock.sleep)
    await first(gen)
    await first(gen)  # an idle poll
    store.event(project, None, "worker.merged", {"task": "ui"})
    delivered = None
    for _ in range(4):
        frame = await first(gen)
        if "event: worker.merged" in frame:
            delivered = frame
            break
        clock.now += POLL_INTERVAL
    assert delivered, "a live event was not forwarded"
    assert json.loads(delivered.split("data: ", 1)[1])["kind"] == "worker.merged"


@run
async def test_the_stream_never_repeats_an_event_it_already_sent(tmp_path):
    """A duplicated frame would render the same worker event twice in the wave view."""
    store = Store(tmp_path / "factory.sqlite3")
    project = make_project(store)
    cursor = store.all("SELECT max(seq) AS s FROM events WHERE project_id=?", (project,))[0]["s"] or 0
    store.event(project, None, "once", {})

    clock = Clock()
    gen = event_frames(fetcher(store, project), project, cursor, clock=clock, sleep=clock.sleep)
    await first(gen)
    ids = []
    for _ in range(10):
        frame = await first(gen)
        clock.now += POLL_INTERVAL
        if frame.startswith("id: "):
            ids.append(frame.split("\n")[0])
    assert len(ids) == len(set(ids)), f"duplicate seq delivered: {ids}"


@run
async def test_events_of_another_project_are_never_streamed(tmp_path):
    """The cursor is per project even though seq is global."""
    store = Store(tmp_path / "factory.sqlite3")
    mine = make_project(store)
    other = make_project(store)
    store.event(other, None, "not.mine", {})

    clock = Clock()
    gen = event_frames(fetcher(store, mine), mine, 0, clock=clock, sleep=clock.sleep)
    for _ in range(6):
        assert "not.mine" not in await first(gen)
        clock.now += POLL_INTERVAL


def test_the_stream_route_is_async_so_it_cannot_exhaust_the_threadpool():
    """A synchronous stream endpoint would occupy an anyio worker thread for its whole life.

    anyio's default pool is 40 threads. A handful of open dashboard tabs would then starve
    every other request in the API, which is a whole-service outage caused by watching a build.
    """
    import inspect

    from factory.api import create_app
    from factory.config import Settings
    from factory.models import Profile

    root = Path(__file__).resolve().parent.parent
    settings = Settings(
        data=Path("/tmp/unused-stream-test"), operator_token="o" * 40, release_token="r" * 40,
        model="test/model", pi="/usr/bin/true",
        profiles={"static": Profile(runner="native", commands=[["true"]],
                                    artifact_dir="index.html",
                                    checks_dir=str(root / "checks"),
                                    checks_argv=[["python3", str(root / "checks" / "div_scan.py")]])})
    settings.validate()
    app = create_app(settings)
    stream_route = next(r for r in app.routes
                        if getattr(r, "path", "") == "/api/projects/{project}/stream")
    assert inspect.iscoroutinefunction(stream_route.endpoint), \
        "the stream endpoint must be `async def`; a sync one holds a threadpool slot forever"
