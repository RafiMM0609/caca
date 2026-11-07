"""Independent tester's bounded, declarative API/browser test contract."""
import json
import time
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import httpx
from pydantic import Field, model_validator

from .models import StrictModel
from .process import Cancelled
from .workspace import atomic_json


class TestAction(StrictModel):
    action: Literal["request", "goto", "fill", "click", "expect_text", "expect_visible"]
    path: str = Field(default="/", max_length=1000)
    selector: str = Field(default="", max_length=500)
    value: str = Field(default="", max_length=4000)
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"] = "GET"
    body: dict | None = None
    status: int = Field(default=200, ge=100, le=599)
    json_equals: dict | None = None

    @model_validator(mode="after")
    def valid_action(self):
        if self.action in ("request", "goto"):
            if (not self.path.startswith("/") or self.path.startswith("//") or "\\" in self.path
                    or any(ord(c) < 32 for c in self.path) or urlsplit(self.path).netloc):
                raise ValueError("Tests must use relative application paths")
        if self.action in ("fill", "click", "expect_text", "expect_visible") and not self.selector:
            raise ValueError("Browser action requires a selector")
        return self


class TestCase(StrictModel):
    flow_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,47}$")
    name: str = Field(min_length=3, max_length=200)
    kind: Literal["api", "browser"]
    actions: list[TestAction] = Field(min_length=1, max_length=30)

    @model_validator(mode="after")
    def meaningful(self):
        if self.kind == "api" and any(step.action != "request" for step in self.actions):
            raise ValueError("API cases contain requests only")
        if self.kind == "browser":
            if self.actions[0].action != "goto" or any(step.action == "request" for step in self.actions):
                raise ValueError("Browser case must start by navigating and contain browser actions only")
            if not any(step.action in ("expect_text", "expect_visible") for step in self.actions):
                raise ValueError("Browser case requires an assertion")
        return self


class TestSuite(StrictModel):
    summary: str = Field(min_length=10, max_length=4000)
    cases: list[TestCase] = Field(min_length=1, max_length=30)

    def validate_coverage(self, flows):
        expected = {flow.id: flow.kind for flow in flows}
        if {case.flow_id for case in self.cases} != set(expected):
            raise ValueError("Tester must cover every planner flow and no invented flow IDs")
        if any(expected[case.flow_id] != case.kind for case in self.cases):
            raise ValueError("Test kind differs from planner flow")


def run_suite(suite, base_url, evidence: Path, timeout=120, cancelled=lambda: False):
    from playwright.sync_api import sync_playwright, expect
    evidence.mkdir(parents=True, exist_ok=True)
    report = {"status": "running", "cases": []}
    deadline = time.monotonic() + timeout
    browser = None
    with sync_playwright() as playwright, httpx.Client(trust_env=False, follow_redirects=False, timeout=5) as client:
        try:
            if any(case.kind == "browser" for case in suite.cases):
                import os
                executable = os.environ.get("FACTORY_CHROMIUM")
                browser = playwright.chromium.launch(headless=True, executable_path=executable or None, timeout=min(timeout, 30) * 1000)
            for index, case in enumerate(suite.cases):
                result = {"flow_id": case.flow_id, "name": case.name, "kind": case.kind, "status": "running"}
                report["cases"].append(result)
                context = page = None
                try:
                    if browser:
                        context = browser.new_context(service_workers="block", accept_downloads=False)
                        context.route("**/*", lambda route: route.continue_() if
                                      urlsplit(route.request.url)[:2] == urlsplit(base_url)[:2] else route.abort())
                        context.route_web_socket("**/*", lambda ws: ws.close())
                        page = context.new_page()
                    for action in case.actions:
                        if cancelled():
                            raise Cancelled("Testing cancelled")
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise TimeoutError("Tester execution deadline exceeded")
                        duration = min(5, remaining)
                        if action.action == "request":
                            with client.stream(action.method, base_url + action.path, json=action.body, timeout=duration) as response:
                                assert response.status_code == action.status, "API status assertion failed"
                                if action.json_equals is not None:
                                    data = bytearray()
                                    for chunk in response.iter_bytes():
                                        data.extend(chunk)
                                        if len(data) > 1_000_000 or time.monotonic() > deadline:
                                            raise ValueError("API response exceeded test limits")
                                    assert json.loads(data) == action.json_equals, "API JSON assertion failed"
                        elif action.action == "goto":
                            page.goto(base_url + action.path, wait_until="domcontentloaded", timeout=duration * 1000)
                        elif action.action == "fill":
                            page.locator(action.selector).fill(action.value, timeout=duration * 1000)
                        elif action.action == "click":
                            page.locator(action.selector).click(timeout=duration * 1000)
                        elif action.action == "expect_text":
                            expect(page.locator(action.selector)).to_contain_text(action.value, timeout=duration * 1000)
                        elif action.action == "expect_visible":
                            expect(page.locator(action.selector)).to_be_visible(timeout=duration * 1000)
                    result["status"] = "passed"
                except BaseException as exc:
                    result["status"] = "cancelled" if isinstance(exc, Cancelled) else "failed"
                    result["error"] = type(exc).__name__
                    raise
                finally:
                    if page and not page.is_closed():
                        try:
                            filename = f"case-{index + 1}.png"
                            page.screenshot(path=str(evidence / filename), timeout=3000)
                            result["screenshot"] = filename
                        except Exception:
                            pass
                    if context:
                        context.close()
            report["status"] = "passed"
        except BaseException:
            report["status"] = "failed"
            raise
        finally:
            if browser:
                browser.close()
            atomic_json(evidence / "report.json", report)
    return report
