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


class Plan(StrictModel):
    summary: str = Field(min_length=10, max_length=12000)
    assumptions: list[str] = Field(default_factory=list, max_length=50)
    questions: list[str] = Field(default_factory=list, max_length=30)
    tasks: list[Task] = Field(min_length=1, max_length=30)

    @model_validator(mode="after")
    def valid_graph(self):
        seen = set()
        for task in self.tasks:
            if task.id in seen or any(dep not in seen for dep in task.depends_on):
                raise ValueError("Tasks must have unique IDs and dependencies earlier in the list")
            seen.add(task.id)
        return self


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
    image: str
    verify_argv: list[str] = Field(min_length=1, max_length=50)
    artifact_dir: str = "dist"
    checks_dir: str
    timeout: int = Field(default=300, ge=10, le=3600)
    memory_mb: int = Field(default=1024, ge=128, le=8192)
    cpus: float = Field(default=1, ge=0.25, le=8)

    @model_validator(mode="after")
    def validate_profile(self):
        safe_path(self.artifact_dir)
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

