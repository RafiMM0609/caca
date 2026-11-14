"""Browser smoke test for the dashboard. Opt-in, because Chromium is expensive here.

**Why this is not in the default suite.** Measured on the host that runs the factory: a Chromium
process tree is ~387 MB resident. That host has 1.9 GiB total, and a build job is already running
`npm ci` and `nuxt build` at roughly 1 GB. Starting Chromium alongside a build is how the OOM
killer gets involved, and an OOM kill during a build is indistinguishable from a real failure in
the job log. So the gate is `FACTORY_UI_TEST=1`, the same shape as `FACTORY_TEST_DOCKER` and
`FACTORY_LIVE_WORKER`: opt-in, documented, and never a default.

**What it covers that the unit tests cannot.** Three defects were found by this test and by
nothing else, which is the argument for keeping it:

  - `/healthz/detail` returned 500 once a lane had heartbeated. The fixture store has no
    heartbeat rows, so no unit test reached the branch.
  - The disabled `Terapkan & uji` button still looked enabled, because 40% opacity on a bright
    green fill reads as "press me" on a dark panel.
  - The Studio help paragraph was still English, and badges printed raw machine states
    ("None", "Failed") at the operator.

None of those is a logic error a pure function test can see; they are properties of a rendered
page. That is exactly the gap this covers.

Run it with:

    FACTORY_UI_TEST=1 uv run pytest tests/test_ui_browser.py -q

It needs `uv run playwright install chromium` once. It does not touch the real data directory:
the store is a throwaway under `tmp_path`.
"""
import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("FACTORY_UI_TEST") != "1",
    reason="set FACTORY_UI_TEST=1 to run the browser smoke test (~390 MB for Chromium)",
)


@pytest.fixture
def served(settings, store, project):
    """The real app on a real socket, because a browser cannot talk to TestClient."""
    import socket
    import threading
    import time

    import uvicorn

    from factory.api import create_app

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = uvicorn.Server(uvicorn.Config(create_app(settings), host="127.0.0.1", port=port,
                                           log_level="error", access_log=False))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(300):
        if server.started:
            break
        time.sleep(0.02)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True


@pytest.fixture
def page(served):
    sync_api = pytest.importorskip("playwright.sync_api",
                                   reason="playwright is not installed")
    with sync_api.sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch()
        except Exception as exc:  # noqa: BLE001 - a missing browser is a skip, not a failure
            pytest.skip(f"Chromium unavailable: {exc}")
        context = browser.new_context(viewport={"width": 1280, "height": 900})
        page = context.new_page()
        page.errors = []
        page.on("pageerror", lambda error: page.errors.append(str(error)))
        page.goto(served)
        page.fill("#token", "o" * 40)
        page.click("#connect-form button")
        page.wait_for_selector("#workspace:not([hidden])", timeout=15000)
        yield page
        browser.close()


def test_the_dashboard_renders_in_indonesian(page):
    assert page.title() == "Anton · Software Factory"
    assert page.get_attribute("html", "lang") == "id"
    assert "Proyek" in page.inner_text(".heading h1")
    assert page.errors == [], f"uncaught JS errors: {page.errors}"


def test_no_javascript_error_on_load(page):
    """A page that logs nothing is the baseline; every later assertion assumes it."""
    assert page.errors == []


def test_the_sidebar_names_the_real_runtime(page):
    assert "Pi 1.5.0" in page.inner_text(".sidebar-note")
    assert "Oh My Pi" not in page.inner_text(".sidebar-note")


def test_a_project_opens_and_shows_a_labelled_state(page):
    page.locator(".project-card").first.click()
    page.wait_for_selector("#detail:not([hidden])", timeout=15000)
    badge = page.inner_text("#project-state")
    # A raw machine state ("awaiting_plan", "None") is not a label. The map is asserted
    # separately; here the point is that what reached the operator is human-readable.
    assert "_" not in badge, f"raw machine state shown to the operator: {badge!r}"


def test_the_disabled_button_looks_disabled(page):
    """Verified against the rendered pixels, because the DOM said disabled and the eye disagreed."""
    page.locator(".project-card").first.click()
    page.wait_for_selector("#detail:not([hidden])", timeout=15000)
    button = page.locator("#studio-apply")
    assert button.is_disabled()
    opacity = button.evaluate("el => getComputedStyle(el).opacity")
    assert float(opacity) < 0.6, "a disabled primary must not look pressable"
    assert "grayscale" in button.evaluate("el => getComputedStyle(el).filter")


def test_the_login_heading_is_localised(page):
    page.click("#logout")
    page.wait_for_selector("#login:not([hidden])", timeout=10000)
    assert "verifikasi" in page.inner_text("#login h1").lower()


def test_the_page_does_not_overflow_a_phone(page):
    """Measured at 390px: the detail view once grew the document to 643px wide."""
    page.locator(".project-card").first.click()
    page.wait_for_selector("#detail:not([hidden])", timeout=15000)
    page.set_viewport_size({"width": 390, "height": 844})
    page.wait_for_timeout(600)
    assert page.evaluate("document.documentElement.scrollWidth") <= 390


def test_a_reload_keeps_the_session(page):
    """The token lives in sessionStorage, so a reload must not force a re-paste."""
    page.reload()
    page.wait_for_selector("#workspace:not([hidden])", timeout=15000)
    assert page.is_visible("#workspace")


def test_the_event_stream_connects_and_delivers(page, store):
    """The dashboard must receive a live event, not just poll for it.

    Driven end to end: the test writes an event through the same store the server reads, then
    waits for the browser's own cursor to move past it. Asserting only that a stream object
    exists would pass even if the parser or the reconnect logic were broken.
    """
    page.locator(".project-card").first.click()
    page.wait_for_selector("#detail:not([hidden])", timeout=15000)
    # Wait for the stream to be established before writing, so the event is not simply replayed
    # from history and mistaken for a live delivery.
    page.wait_for_function("() => typeof streamActive !== 'undefined' && streamActive === true",
                           timeout=15000)
    before = page.evaluate("eventCursor")
    project = page.evaluate("current")
    store.event(project, None, "ui.probe", {"from": "test_ui_browser"})
    page.wait_for_function(
        "([before]) => eventCursor > before", arg=[before], timeout=15000)
    after = page.evaluate("eventCursor")
    assert after > before, "a live event did not advance the cursor"
    kinds = page.evaluate("eventLog.map(e => e.kind)")
    assert "ui.probe" in kinds, f"the live event was not recorded: {kinds[-5:]}"
