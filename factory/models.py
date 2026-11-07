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


def safe_path(name: str) -> str:
    path = PurePosixPath(name)
    if (not name or name == "." or len(name) > 240 or path.is_absolute() or "\\" in name
            or any(ord(c) < 32 for c in name) or str(path) != name
            or any(p in ("..", ".git", ".omp", ".memorix", ".ssh") for p in path.parts)
            or any(p == ".env" or p.startswith(".env.") for p in path.parts)):
        raise ValueError(f"Disallowed file path: {name!r}")
    return name


class Change(StrictModel):
    summary: str = Field(min_length=3, max_length=4000)
    files: dict[str, str] = Field(max_length=100)
    delete: list[str] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def valid_files(self):
        if not self.files and not self.delete:
            raise ValueError("Empty change")
        for name in [*self.files, *self.delete]:
            safe_path(name)
        if set(self.files) & set(self.delete):
            raise ValueError("Cannot write and delete the same file")
        if sum(len(v.encode()) for v in self.files.values()) > 2_000_000:
            raise ValueError("Change exceeds 2 MB")
        return self


class Review(StrictModel):
    approved: bool
    findings: list[str] = Field(max_length=100)


class ProjectInput(StrictModel):
    name: str = Field(min_length=3, max_length=100)
    brief: str = Field(min_length=20, max_length=20000)
    profile: str = Field(default="static", pattern=r"^[a-z][a-z0-9_-]{0,40}$")


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
    studio: bool = False
    studio_install_argv: list[str] = Field(default_factory=lambda: ["npm", "ci"])
    studio_dev_argv: list[str] = Field(default_factory=lambda: ["npm", "run", "dev", "--", "--host", "127.0.0.1", "--port", "{port}"])

    @model_validator(mode="after")
    def validate_profile(self):
        safe_path(self.artifact_dir)
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

