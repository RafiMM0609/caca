"""Opt-in real Nuxt/Studio/browser journey. AI responses are deterministic test doubles.
Run with FACTORY_ALLOW_NATIVE=1 and a locally installed Playwright browser.
"""

import json
import os
from pathlib import Path
import secrets
import socket
import threading
import time

import uvicorn
from playwright.sync_api import sync_playwright, expect

from factory.api import create_app
from factory.config import Settings
from factory.db import Store
from factory.engine import Engine
from factory.models import Profile
from factory.testing import TestSuite as Suite


class ContractTester:
    def generate(self, role, instruction, context, schema, cancelled):
        assert role == "tester", "Starter/content verification must not invoke implementer"
        return Suite(
            summary="Real API contracts and visitor browser journey from the supplied starter plan.",
            cases=[
                {
                    "flow_id": "health",
                    "name": "App health contract",
                    "kind": "api",
                    "actions": [
                        {"action": "request", "path": "/api/health", "json_equals": {"status": "ok"}}
                    ],
                },
                {
                    "flow_id": "contact-validation",
                    "name": "Contact validation contract",
                    "kind": "api",
                    "actions": [
                        {
                            "action": "request",
                            "method": "POST",
                            "path": "/api/contact",
                            "body": {"email": "person@example.test"},
                            "json_equals": {"message": "Thanks! Your demo enquiry is valid."},
                        },
                        {
                            "action": "request",
                            "method": "POST",
                            "path": "/api/contact",
                            "body": {"email": "invalid"},
                            "status": 400,
                        },
                    ],
                },
                {
                    "flow_id": "contact-journey",
                    "name": "Visitor contact journey",
                    "kind": "browser",
                    "actions": [
                        {"action": "goto"},
                        {"action": "expect_visible", "selector": "[data-testid=hero-title]"},
                        {"action": "fill", "selector": "#email", "value": "person@example.test"},
                        {"action": "click", "selector": "button[type=submit]"},
                        {
                            "action": "expect_text",
                            "selector": "[data-testid=contact-result]",
                            "value": "Thanks! Your demo enquiry is valid.",
                        },
                    ],
                },
            ],
        )


def main():
    root = Path(__file__).resolve().parents[1]
    destination = root / "data" / "validation"
    destination.mkdir(parents=True, exist_ok=True)
    data = destination / ("run-" + secrets.token_hex(4))
    profile = Profile.model_validate(
        json.loads((root / "examples/config-studio-linux.json").read_text())["profiles"]["nuxt-studio"]
    )
    settings = Settings(
        data=data,
        operator_token=secrets.token_urlsafe(32),
        release_token=secrets.token_urlsafe(32),
        profiles={"nuxt-studio": profile},
        model="test-double/not-a-provider",
    )
    settings.validate()
    store = Store(data / "factory.sqlite3")
    engine = Engine(settings, store, agent=ContractTester())
    stop = threading.Event()

    def worker():
        while not stop.is_set():
            store.heartbeat()
            job = store.claim()
            if job:
                print("Executing", job["kind"], job["id"], flush=True)
                engine.process(job)
                print(
                    "Finished", store.one("SELECT state,error FROM jobs WHERE id=?", (job["id"],)), flush=True
                )
            else:
                stop.wait(0.2)

    worker_thread = threading.Thread(target=worker, daemon=True)
    worker_thread.start()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(create_app(settings), host="127.0.0.1", port=port, log_level="error")
    )
    api_thread = threading.Thread(target=server.run, daemon=True)
    api_thread.start()
    while not server.started:
        time.sleep(0.1)

    def wait_for_build(page):
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            rows = store.all("SELECT state FROM projects")
            if rows and rows[0]["state"] == "failed":
                job = store.one("SELECT * FROM jobs ORDER BY created DESC LIMIT 1")
                log = data / "jobs" / job["id"] / "verification.txt"
                raise AssertionError(log.read_text()[-4000:] if log.exists() else job["error"])
            if rows and rows[0]["state"] == "ready":
                break
            page.wait_for_timeout(500)
        expect(page.locator("#project-state")).to_have_text("ready", timeout=15000)

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                executable_path=os.environ.get("FACTORY_CHROMIUM") or None, headless=True
            )
            page = browser.new_page(viewport={"width": 1500, "height": 1100})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            try:
                page.goto(f"http://127.0.0.1:{port}")
                page.get_by_label("Operator token", exact=True).fill(settings.operator_token)
                page.get_by_role("button", name="Connect to factory").click()
                page.get_by_role("button", name="New project").click()
                page.get_by_label("Project name").fill("Studio acceptance demo")
                page.get_by_label("Brief and acceptance criteria").fill(
                    "Create an editable website and validate its visitor contact journey."
                )
                page.get_by_role("button", name="Create project", exact=True).click()
                page.get_by_role("button", name="Create & test Studio starter").click()
                wait_for_build(page)
                project = store.one("SELECT * FROM projects")
                first = json.loads(project["artifact"])
                print("First verified Nuxt build", first["job_id"], flush=True)
                page.get_by_role("button", name="Open editable draft").click()
                expect(page.locator("#studio-state")).to_have_text("editing", timeout=240000)
                frame = page.frame_locator("#studio-frame")
                expect(frame.get_by_test_id("hero-title")).to_be_visible(timeout=30000)
                page.locator("#studio-frame").scroll_into_view_if_needed()
                toggle = frame.locator("nuxt-studio").get_by_role("button").last
                toggle.wait_for()
                print(
                    "Studio launcher",
                    toggle.evaluate(
                        "el => ({rect:el.getBoundingClientRect().toJSON(),height:window.innerHeight,width:window.innerWidth})"
                    ),
                    flush=True,
                )
                toggle.click()
                frame.get_by_text("site.json", exact=True).click()
                frame.get_by_label("Title", exact=True).fill("Edited directly from the factory dashboard.")
                frame.get_by_label("Title", exact=True).press("Tab")
                draft = store.one("SELECT * FROM studio_drafts")
                content = data / "studio" / draft["id"] / "source" / "content" / "site.json"
                end = time.monotonic() + 30
                while time.monotonic() < end:
                    if (
                        json.loads(content.read_text())["title"]
                        == "Edited directly from the factory dashboard."
                    ):
                        break
                    time.sleep(0.2)
                assert (
                    json.loads(content.read_text())["title"] == "Edited directly from the factory dashboard."
                )
                expect(frame.get_by_test_id("hero-title")).to_have_text(
                    "Edited directly from the factory dashboard.", timeout=15000
                )
                page.locator("#studio-panel").screenshot(path=str(destination / "studio-editing.png"))
                page.get_by_role("button", name="Review changes", exact=True).click()
                expect(page.locator("#studio-diff-text")).to_contain_text("Edited directly", timeout=30000)
                page.get_by_role("button", name="Apply & test", exact=True).click()
                expect(page.locator("#project-profile")).to_contain_text("REVISION 2", timeout=15000)
                wait_for_build(page)
                latest = store.project(project["id"])
                second = json.loads(latest["artifact"])
                assert second["revision"] == 2 and second["job_id"] != first["job_id"]
                assert (data / "jobs" / first["job_id"] / "artifact.tar.gz").exists()
                original = json.loads(
                    (data / "jobs" / first["job_id"] / "source" / "content" / "site.json").read_text()
                )
                changed = json.loads(
                    (data / "jobs" / second["job_id"] / "source" / "content" / "site.json").read_text()
                )
                assert original["title"] != changed["title"]
                report = json.loads(
                    (data / "jobs" / second["job_id"] / "testing" / "report.json").read_text()
                )
                assert report["status"] == "passed" and len(report["cases"]) == 3
                page.screenshot(path=str(destination / "dashboard-verified.png"), full_page=True)
                page.set_viewport_size({"width": 390, "height": 844})
                assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
                page.screenshot(path=str(destination / "dashboard-mobile.png"), full_page=True)
                assert not errors, errors
                result = {
                    "status": "passed",
                    "ai": "scripted test double; no live model call",
                    "revision": 2,
                    "api_and_browser_cases": 3,
                    "initial_job": first["job_id"],
                    "edited_job": second["job_id"],
                    "journey": "dashboard create -> real Nuxt build -> API/browser tests -> embedded Nuxt Studio edit -> diff -> apply -> real rebuild -> API/browser retest",
                    "data": str(data),
                }
                (destination / "studio-journey.json").write_text(json.dumps(result, indent=2))
                print(json.dumps(result, indent=2), flush=True)
            except BaseException:
                page.screenshot(path=str(destination / "journey-failure.png"), full_page=True)
                raise
            finally:
                browser.close()
    finally:
        stop.set()
        engine.stop.set()
        server.should_exit = True
        worker_thread.join(timeout=20)
        api_thread.join(timeout=20)


if __name__ == "__main__":
    main()
