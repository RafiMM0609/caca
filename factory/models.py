import re
from pathlib import PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Task(StrictModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,47}$")
    title: str = Field(min_length=3, max_length=200)
    instructions: str = Field(min_length=10, max_length=12000)
    acceptance: list[str] = Field(min_length=1, max_length=30)
    depends_on: list[str] = Field(default_factory=list, max_length=30)


class AcceptanceFlow(StrictModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,47}$")
    kind: Literal["api", "browser"]
    goal: str = Field(min_length=10, max_length=2000)
    steps: list[str] = Field(min_length=1, max_length=20)
    # Semantic name -> CSS selector, declared once in the plan and shared by the workers that
    # implement the page and the tester that drives it. Without a single source of truth the
    # two guessed differently and disagreed: a worker shipped `edit-input-name` for a row in
    # edit mode while the tester asserted `input-name`, so a correct page failed the gate.
    selectors: dict[str, str] = Field(default_factory=dict, max_length=60)

    @model_validator(mode="before")
    @classmethod
    def normalize_selector_names(cls, data):
        """Accept `camelCase` selector names by rewriting them to `snake_case`.

        The key of a `selectors` entry is a label, not a CSS construct, so the style a model
        reaches for is arbitrary and both spellings are unambiguous. Rejecting `shopName`
        outright failed live planner jobs twice in a row: the model produced a correct plan
        and the whole job died on a naming convention nothing had told it about. Normalising
        is cheaper and safer than spending a repair attempt on a spelling.
        """
        if isinstance(data, dict) and isinstance(data.get("selectors"), dict):
            data = {**data, "selectors": {
                re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower(): value
                for name, value in data["selectors"].items()}}
        return data

    @model_validator(mode="after")
    def validate_selectors(self):
        for name, selector in self.selectors.items():
            if not re.fullmatch(r"[a-z][a-z0-9_-]{0,47}", name):
                raise ValueError(f"Invalid selector name: {name!r} (use lowercase snake_case)")
            if not selector or len(selector) > 300:
                raise ValueError(f"Invalid selector for {name}")
            if any(ord(c) < 32 for c in selector) or "url(" in selector.lower():
                raise ValueError(f"Invalid selector for {name}")
        if self.kind == "browser" and not self.selectors:
            # A browser flow that names no selectors forces the tester to invent them.
            raise ValueError("Browser flows must declare the selectors they rely on")
        return self


class Plan(StrictModel):
    summary: str = Field(min_length=10, max_length=12000)
    assumptions: list[str] = Field(default_factory=list, max_length=50)
    questions: list[str] = Field(default_factory=list, max_length=30)
    tasks: list[Task] = Field(min_length=1, max_length=30)
    test_flows: list[AcceptanceFlow] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def valid_graph(self):
        """Tasks form a DAG: dependencies may point anywhere, but must not cycle.

        Execution order is derived from the graph, not from list position, so a planner
        can declare independent tasks that run in parallel.
        """
        ids = {task.id for task in self.tasks}
        if len(ids) != len(self.tasks):
            raise ValueError("Task IDs must be unique")
        for task in self.tasks:
            if task.id in task.depends_on:
                raise ValueError(f"Task {task.id} cannot depend on itself")
            unknown = [dep for dep in task.depends_on if dep not in ids]
            if unknown:
                raise ValueError(f"Task {task.id} depends on unknown tasks: {', '.join(unknown)}")
        if len({flow.id for flow in self.test_flows}) != len(self.test_flows):
            raise ValueError("Test flow IDs must be unique")
        self.execution_order()
        return self

    def _dependencies(self) -> dict[str, list[str]]:
        return {task.id: list(task.depends_on) for task in self.tasks}

    def execution_order(self) -> list[str]:
        """Topological order. Raises on a cycle instead of silently dropping tasks."""
        deps = self._dependencies()
        ordered: list[str] = []
        while deps:
            ready = sorted(name for name, needs in deps.items() if not any(d in deps for d in needs))
            if not ready:
                raise ValueError("Task dependencies contain a cycle")
            for name in ready:
                ordered.append(name)
                deps.pop(name)
        return ordered

    def ready_set(self, completed) -> list[Task]:
        """Tasks whose dependencies are all satisfied and that are not yet completed."""
        done = set(completed)
        return [task for task in self.tasks
                if task.id not in done and all(dep in done for dep in task.depends_on)]

    def ordered_tasks(self) -> list[Task]:
        by_id = {task.id: task for task in self.tasks}
        return [by_id[name] for name in self.execution_order()]


# Example and template env files carry no secret; they are the documented way to show which
# variables an app needs. Blocking them outright made every app that ships an `.env.example`
# unbuildable, which is most real applications.
ENV_TEMPLATE_SUFFIXES = (".example", ".sample", ".template", ".dist")


def _is_real_env_file(part: str) -> bool:
    """`.env`, `.env.local` and `.env.production` are secrets; `.env.example` is documentation."""
    if part != ".env" and not part.startswith(".env."):
        return False
    return not part.endswith(ENV_TEMPLATE_SUFFIXES)


# Paths a generated application must never contain. `.git` is here because a *source* tree
# is authored by a worker: a `.git` there could carry hooks, a config with credentials, or
# objects that confuse the product repository.
#
# A published *artifact* is different. It is built from tracked files by this factory and
# its `.git` is created fresh by `git init` inside the artifact directory, so it holds only
# history the factory just wrote. `safe_path(..., allow_git=True)` covers exactly that
# packaging path and nothing else.
BLOCKED_PATH_PARTS = ("..", ".git", ".pi", ".memorix", ".ssh")


def safe_path(name: str, allow_git: bool = False) -> str:
    path = PurePosixPath(name)
    blocked = BLOCKED_PATH_PARTS if not allow_git else tuple(
        part for part in BLOCKED_PATH_PARTS if part != ".git")
    if (not name or name == "." or len(name) > 240 or path.is_absolute() or "\\" in name
            or any(ord(c) < 32 for c in name) or str(path) != name
            or any(p in blocked for p in path.parts)
            or any(_is_real_env_file(p) for p in path.parts)):
        raise ValueError(f"Disallowed file path: {name!r}")
    return name


class Review(StrictModel):
    approved: bool
    findings: list[str] = Field(max_length=100)


class ProjectInput(StrictModel):
    name: str = Field(min_length=3, max_length=100)
    brief: str = Field(min_length=20, max_length=20000)
    profile: str = Field(default="static", pattern=r"^[a-z][a-z0-9_-]{0,40}$")


# A profile's env is operator-authored configuration, not a place for secrets. It reaches a
# generated application, which is the least trusted process in the factory, so a credential
# pasted here would be handed straight to code the factory itself wrote. Secrets belong in the
# operator's environment, and generated apps must not receive factory or provider tokens.
WITHHELD_ENV_KEYS = frozenset({
    "FACTORY_OPERATOR_TOKEN", "FACTORY_RELEASE_TOKEN", "FACTORY_DATA", "FACTORY_CONFIG",
    "FACTORY_PI", "FACTORY_MODEL", "FACTORY_CHECKS_DIR",
    "OPENCODE_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_OAUTH_TOKEN", "OPENAI_API_KEY",
    "GEMINI_API_KEY", "OPENROUTER_API_KEY", "ZAI_API_KEY", "MINIMAX_API_KEY",
    "DEEPSEEK_API_KEY", "KIMI_API_KEY", "COPILOT_GITHUB_TOKEN",
})
ALLOWED_ENV_PLACEHOLDERS = ("{data}", "{app}")


def _validate_profile_env(env: dict[str, str]) -> None:
    for name, value in env.items():
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", name):
            raise ValueError(f"Invalid profile environment name: {name!r}")
        if name in WITHHELD_ENV_KEYS or name.startswith("FACTORY_"):
            raise ValueError(f"Profile environment may not set {name}; it is reserved")
        if any(ord(c) < 32 or ord(c) == 127 for c in value) or len(value) > 4096:
            raise ValueError(f"Profile environment value for {name} is invalid")
        for token in re.findall(r"\{[^}]*\}", value):
            if token not in ALLOWED_ENV_PLACEHOLDERS:
                raise ValueError(f"Unknown placeholder {token} in profile environment for {name}")


class Profile(StrictModel):
    runner: Literal["docker", "native"] = "docker"
    commands: list[list[str]] = Field(default_factory=list, max_length=20)
    image: str = ""
    verify_argv: list[str] = Field(default_factory=list, max_length=50)
    artifact_dir: str = "dist"
    checks_dir: str = ""
    # Native-only operator-owned checks, run after the build in the disposable tree.
    checks_argv: list[list[str]] = Field(default_factory=list, max_length=10)
    timeout: int = Field(default=300, ge=10, le=3600)
    memory_mb: int = Field(default=1024, ge=128, le=8192)
    cpus: float = Field(default=1, ge=0.25, le=8)
    tester: bool = False
    serve_argv: list[str] = Field(default_factory=list, max_length=30)
    readiness_path: str = "/"
    qa_timeout: int = Field(default=120, ge=10, le=600)
    # Extra environment for the build, operator checks and the served application.
    # `{data}` expands to the project's persistent data directory, so a generated app can
    # keep a SQLite file that survives restarts. Values may also use `{app}` (build tree).
    env: dict[str, str] = Field(default_factory=dict)
    studio: bool = False
    studio_install_argv: list[str] = Field(default_factory=lambda: ["npm", "ci"])
    studio_dev_argv: list[str] = Field(default_factory=lambda: ["npm", "run", "dev", "--", "--host", "127.0.0.1", "--port", "{port}"])

    @model_validator(mode="after")
    def validate_profile(self):
        safe_path(self.artifact_dir)
        _validate_profile_env(self.env)
        if self.tester and (self.runner != "native" or not self.serve_argv):
            raise ValueError("Tester currently requires native runner and serve_argv")
        if self.studio and self.runner != "native":
            raise ValueError("Local Studio requires a native profile")
        if not self.readiness_path.startswith("/") or self.readiness_path.startswith("//"):
            raise ValueError("Readiness must use an application-relative URL")
        if self.runner == "native":
            if not self.commands or any(not command or any(not arg for arg in command) for command in self.commands):
                raise ValueError("Native profiles require nonempty command argument arrays")
            if self.checks_argv and not self.checks_dir:
                raise ValueError("Native checks_argv requires an operator-owned checks_dir")
            # A profile that names a checks directory but lists no checks looks validated
            # while running none. That exact mismatch shipped before: `checks_dir` was only
            # ever read by the Docker verifier, so the active native profile ran zero
            # operator checks and nothing said so. Refusing the combination makes the
            # mistake a startup error instead of a false sense of coverage.
            if self.checks_dir and not self.checks_argv:
                raise ValueError("Native checks_dir requires checks_argv; a checks directory "
                                 "with no checks runs nothing while appearing validated")
            return self
        if not self.verify_argv or not self.checks_dir:
            raise ValueError("Docker profiles require checks and verify_argv")
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9./:_-]*@sha256:[a-f0-9]{64}", self.image):
            raise ValueError("Verification image must be pinned by sha256 digest")
        return self


class Target(StrictModel):
    host: str = Field(pattern=r"^[a-z_][a-z0-9_-]*@[a-zA-Z0-9.-]+$")
    identity_file: str
    known_hosts: str
    helper: str = Field(default="/usr/local/bin/sf-release", pattern=r"^/[a-zA-Z0-9/_-]+$")
    port: int = Field(default=22, ge=1, le=65535)


class DeployInput(StrictModel):
    environment: Literal["staging", "production"]
    digest: str = Field(pattern=r"^[a-f0-9]{64}$")

