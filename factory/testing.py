"""Independent tester's bounded, declarative API/browser test contract."""
import json
import re
import time
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import httpx
from pydantic import Field, model_validator

from .models import StrictModel
from .process import Cancelled
from .workspace import atomic_json

# Actions that assert something about the page. A browser case needs at least one.
BROWSER_ASSERTIONS = ("expect_text", "expect_value", "expect_visible", "expect_hidden",
                      "expect_count", "expect_url")

# Keyboard presses are limited to key names, never free text or shell-like input.
KEY_NAME = re.compile(r"^[A-Za-z0-9+_-]{1,30}$")

# Console noise produced by our own network policy (external requests are aborted on purpose).
IGNORABLE_CONSOLE = ("Failed to load resource", "net::ERR_", "favicon.ico", "ERR_BLOCKED_BY_CLIENT")


class TestAction(StrictModel):
    action: Literal["request", "goto", "reload", "fill", "click", "select", "press",
                    "expect_text", "expect_value", "expect_visible", "expect_hidden",
                    "expect_count", "expect_url"]
    path: str = Field(default="/", max_length=1000)
    selector: str = Field(default="", max_length=500)
    value: str = Field(default="", max_length=4000)
    method: Literal["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"] = "GET"
    body: dict | None = None
    status: int = Field(default=200, ge=100, le=599)
    json_equals: dict | None = None
    count: int = Field(default=-1, ge=-1, le=100_000)

    @model_validator(mode="before")
    @classmethod
    def accept_documented_url_position(cls, data):
        """`expect_url` takes its route in `value`, but a model reaching for `path` is natural.

        `goto` and `reload` carry their route in `path`, so a planner or tester model routinely
        writes `{"action": "expect_url", "path": "/"}`. That is unambiguous and safe, so accept it
        instead of rejecting a whole test suite and spending a repair attempt on a spelling. Only a
        missing `value` is filled in; an explicit `value` always wins.
        """
        if isinstance(data, dict) and data.get("action") == "expect_url" and not data.get("value"):
            path = data.get("path")
            if isinstance(path, str) and path:
                return {**data, "value": path}
        return data

    @model_validator(mode="after")
    def valid_action(self):
        if self.action in ("request", "goto"):
            if (not self.path.startswith("/") or self.path.startswith("//") or "\\" in self.path
                    or any(ord(c) < 32 for c in self.path) or urlsplit(self.path).netloc):
                raise ValueError("Tests must use relative application paths")
        if self.action == "expect_url":
            # Compared against the running application, never an external origin.
            if (not self.value.startswith("/") or self.value.startswith("//") or "\\" in self.value
                    or urlsplit(self.value).netloc):
                raise ValueError("Expected URLs must be relative application paths")
        if self.action in ("fill", "click", "select", "expect_text", "expect_value",
                           "expect_visible", "expect_hidden", "expect_count") and not self.selector:
            raise ValueError("Browser action requires a selector")
        if self.action == "expect_text" and not self.value.strip():
            # An empty expected text matches any element, so it asserts nothing. A tester
            # reaching for "the field is empty" wants expect_value, not expect_text.
            raise ValueError("expect_text requires non-empty text; use expect_value for an "
                             "empty form field")
        if self.action == "press" and not KEY_NAME.fullmatch(self.value):
            raise ValueError("Press must name a single key such as Enter or Control+A")
        if self.action == "expect_count" and self.count < 0:
            raise ValueError("expect_count requires a non-negative count")
        if self.action == "request" and self.method == "HEAD" and self.json_equals is not None:
            raise ValueError("HEAD responses have no body to compare")
        return self


class TestCase(StrictModel):
    flow_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,47}$")
    name: str = Field(min_length=3, max_length=200)
    kind: Literal["api", "browser"]
    actions: list[TestAction] = Field(min_length=1, max_length=30)
    # Escape hatch for a page with a known, reviewed console error. Default is strict.
    allow_console_errors: bool = False

    @model_validator(mode="after")
    def meaningful(self):
        if self.kind == "api" and any(step.action != "request" for step in self.actions):
            raise ValueError("API cases contain requests only")
        if self.kind == "browser":
            if self.actions[0].action != "goto" or any(step.action == "request" for step in self.actions):
                raise ValueError("Browser case must start by navigating and contain browser actions only")
            if not any(step.action in BROWSER_ASSERTIONS for step in self.actions):
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


def ignorable_console(text):
    return any(marker in text for marker in IGNORABLE_CONSOLE)


def run_suite(suite, base_url, evidence: Path, timeout=120, cancelled=lambda: False,
              isolate=None):
    """Execute every case and return a report.

    ``isolate`` is an optional callable(context) -> (base_url, cleanup). It is invoked before
    each case and must return a freshly started application with an empty database, plus a
    function that stops it. Without it, every case shares one process and one database.

    Sharing state is wrong for any writable application. A suite whose first case inserts a
    record and whose second case asserts on that record's id or on the total count then only
    passes by luck: the first case inserted `id: 1`, so the second received `id: 2` and failed
    even though the application was correct. Each case must start from the same known-empty
    state, otherwise the tests measure the previous case instead of the contract.
    """
    from playwright.sync_api import sync_playwright, expect
    evidence.mkdir(parents=True, exist_ok=True)
    report = {"status": "running", "cases": []}
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
                console_errors: list[str] = []
                page_errors: list[str] = []
                case_url = base_url
                cleanup = None
                # `timeout` bounds one case, not the whole suite. A shared budget meant a
                # later case timed out purely because earlier cases had spent it, and with
                # per-case isolation each case also pays for starting its own process, so a
                # suite-wide deadline turned a correct application into a TimeoutError.
                case_deadline = time.monotonic() + timeout
                try:
                    if isolate is not None:
                        if cancelled():
                            raise Cancelled("Testing cancelled")
                        case_url, cleanup = isolate({"flow_id": case.flow_id, "kind": case.kind,
                                                     "index": index, "case": case})
                        result["isolated"] = True
                    if browser:
                        context = browser.new_context(service_workers="block", accept_downloads=False)
                        context.route("**/*", lambda route: route.continue_() if
                                      urlsplit(route.request.url)[:2] == urlsplit(case_url)[:2] else route.abort())
                        context.route_web_socket("**/*", lambda ws: ws.close())
                        page = context.new_page()
                        page.on("console", lambda message: console_errors.append(message.text)
                                if message.type == "error" else None)
                        page.on("pageerror", lambda error: page_errors.append(str(error)))
                    for action in case.actions:
                        if cancelled():
                            raise Cancelled("Testing cancelled")
                        remaining = case_deadline - time.monotonic()
                        if remaining <= 0:
                            raise TimeoutError("Tester case deadline exceeded")
                        duration = min(5, remaining)
                        if action.action == "request":
                            with client.stream(action.method, case_url + action.path, json=action.body, timeout=duration) as response:
                                assert response.status_code == action.status, "API status assertion failed"
                                if action.json_equals is not None:
                                    data = bytearray()
                                    for chunk in response.iter_bytes():
                                        data.extend(chunk)
                                        if len(data) > 1_000_000 or time.monotonic() > case_deadline:
                                            raise ValueError("API response exceeded test limits")
                                    assert json.loads(data) == action.json_equals, "API JSON assertion failed"
                        elif action.action == "goto":
                            page.goto(case_url + action.path, wait_until="domcontentloaded", timeout=duration * 1000)
                        elif action.action == "reload":
                            page.reload(wait_until="domcontentloaded", timeout=duration * 1000)
                        elif action.action == "fill":
                            page.locator(action.selector).fill(action.value, timeout=duration * 1000)
                        elif action.action == "click":
                            page.locator(action.selector).click(timeout=duration * 1000)
                        elif action.action == "select":
                            page.locator(action.selector).select_option(action.value, timeout=duration * 1000)
                        elif action.action == "press":
                            page.keyboard.press(action.value)
                        elif action.action == "expect_text":
                            # "The page shows this text" must hold for whatever the selector
                            # matches: a list of three service cards is a legitimate target, and
                            # the assertion should pass when ANY of them shows the copy. Checking
                            # only the first element failed a correct page, because "Furniture
                            # restoration" lives in the second card. Matching is also
                            # case-insensitive so capitalisation alone cannot fail the test.
                            pattern = re.compile(re.escape(action.value.strip()), re.IGNORECASE)
                            locator = page.locator(action.selector)
                            expect(locator).not_to_have_count(0, timeout=duration * 1000)
                            counts = locator.count()
                            if counts == 1:
                                expect(locator.first).to_contain_text(pattern, timeout=duration * 1000)
                            else:
                                deadline_text = time.monotonic() + duration
                                while True:
                                    texts = [locator.nth(index).inner_text() for index in range(counts)]
                                    if any(pattern.search(text) for text in texts):
                                        break
                                    if time.monotonic() > deadline_text:
                                        raise AssertionError(
                                            f"None of {counts} elements matched {action.selector} contained "
                                            f"the expected text {action.value!r}")
                                    time.sleep(0.1)
                        elif action.action == "expect_value":
                            # A form field's content lives in its `value` attribute, never in its
                            # text content. Asserting "the field is empty after submit" through
                            # expect_text was vacuous: an input's text is always empty, so the
                            # check either passed for the wrong reason or failed on an empty
                            # pattern. Compare the real value instead.
                            locator = page.locator(action.selector)
                            expect(locator).not_to_have_count(0, timeout=duration * 1000)
                            # Exact match: a field value is compared as data, not as prose, so
                            # case folding would hide a wrong value rather than reveal it.
                            expect(locator.first).to_have_value(action.value, timeout=duration * 1000)
                        elif action.action == "expect_visible":
                            # "Visible" means every element the selector matches is visible, not
                            # "the selector resolves to exactly one element". Playwright's strict
                            # mode fails a locator matching three service cards, so a correct page
                            # with three visible cards failed a reasonable assertion. An empty
                            # match must still fail, so first wait for the count to be non-zero.
                            locator = page.locator(action.selector)
                            expect(locator).not_to_have_count(0, timeout=duration * 1000)
                            for index in range(locator.count()):
                                expect(locator.nth(index)).to_be_visible(timeout=duration * 1000)
                        elif action.action == "expect_hidden":
                            locator = page.locator(action.selector)
                            for index in range(locator.count()):
                                expect(locator.nth(index)).to_be_hidden(timeout=duration * 1000)
                        elif action.action == "expect_count":
                            expect(page.locator(action.selector)).to_have_count(action.count, timeout=duration * 1000)
                        elif action.action == "expect_url":
                            actual, wanted = urlsplit(page.url), urlsplit(case_url + action.value)
                            assert (actual.path, actual.query) == (wanted.path, wanted.query), \
                                f"Expected path {wanted.path}, observed {actual.path}"
                    if case.kind == "browser":
                        # An uncaught exception is a real defect; a blocked external resource is our own policy.
                        real_console = [text for text in console_errors if not ignorable_console(text)]
                        result["console_errors"] = real_console
                        result["page_errors"] = page_errors
                        if page_errors:
                            raise AssertionError("Uncaught page exception: " + page_errors[0][:300])
                        if real_console and not case.allow_console_errors:
                            raise AssertionError("Console error: " + real_console[0][:300])
                    result["status"] = "passed"
                except BaseException as exc:
                    result["status"] = "cancelled" if isinstance(exc, Cancelled) else "failed"
                    result["error"] = type(exc).__name__
                    if console_errors:
                        result.setdefault("console_errors", console_errors)
                    if page_errors:
                        result.setdefault("page_errors", page_errors)
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
                    # Stop the isolated application and drop its database before the next
                    # case starts, so no case can observe another case's writes.
                    if cleanup is not None:
                        try:
                            cleanup()
                        except Exception:
                            pass
            report["status"] = "passed"
        except BaseException:
            report["status"] = "failed"
            raise
        finally:
            if browser:
                browser.close()
            atomic_json(evidence / "report.json", report)
    return report
