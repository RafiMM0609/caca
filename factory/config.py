import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from .models import Profile, Target


@dataclass
class Settings:
    data: Path
    operator_token: str
    release_token: str
    pi: str = "pi"
    model: str = ""
    review_model: str = ""
    tester_model: str = ""
    max_cost: float = 2.0
    max_job_cost: float = 20.0
    agent_timeout: int = 600
    # A worker edits a tree, may run `npm ci` and a build to check its own work, and is then
    # reviewed. That is not one model call: 600s routinely expired mid-task. Separate from
    # `agent_timeout` so the cheap structured calls stay tightly bounded while a harness
    # worker gets the time its job needs.
    worker_timeout: int = 1800
    # Wall-clock ceiling for one job. `agent_timeout` bounds a single model invocation; a
    # build is four workers in waves plus a build, acceptance tests and a repair loop, so the
    # two cannot share a number. 15 minutes was a measured Phase 0 build; two hours leaves
    # room for a repair pass without letting a wedged job run forever.
    job_timeout: int = 7200
    # Hard cap for the scaled estimate, so a large plan cannot claim an unbounded run.
    max_job_timeout: int = 21600
    max_repairs: int = 2
    memorix: str = ""
    # How many Pi harness workers may run at once. Each is a separate process tree.
    worker_concurrency: int = 3
    profiles: dict[str, Profile] = field(default_factory=dict)
    targets: dict[str, Target] = field(default_factory=dict)

    @classmethod
    def from_env(cls):
        data = Path(os.environ.get("FACTORY_DATA", "data")).resolve()
        config = Path(os.environ.get("FACTORY_CONFIG", str(data / "config.json")))
        raw = json.loads(config.read_text()) if config.exists() else {}
        return cls(
            data=data,
            operator_token=os.environ.get("FACTORY_OPERATOR_TOKEN", ""),
            release_token=os.environ.get("FACTORY_RELEASE_TOKEN", ""),
            pi=os.environ.get("FACTORY_PI", "pi"),
            model=os.environ.get("FACTORY_MODEL", ""),
            review_model=os.environ.get("FACTORY_REVIEW_MODEL", ""),
            tester_model=os.environ.get("FACTORY_TESTER_MODEL", ""),
            max_cost=float(os.environ.get("FACTORY_MAX_COST", "2")),
            max_job_cost=float(os.environ.get("FACTORY_MAX_JOB_COST", "20")),
            agent_timeout=int(os.environ.get("FACTORY_AGENT_TIMEOUT", "600")),
            worker_timeout=int(os.environ.get("FACTORY_WORKER_TIMEOUT", "1800")),
            job_timeout=int(os.environ.get("FACTORY_JOB_TIMEOUT", "7200")),
            max_job_timeout=int(os.environ.get("FACTORY_MAX_JOB_TIMEOUT", "21600")),
            memorix=os.environ.get("FACTORY_MEMORIX", ""),
            worker_concurrency=int(os.environ.get("FACTORY_WORKER_CONCURRENCY", "3")),
            profiles={k: Profile.model_validate(v) for k, v in raw.get("profiles", {}).items()},
            targets={k: Target.model_validate(v) for k, v in raw.get("targets", {}).items()},
        )

    def job_deadline_seconds(self, plan=None, profile=None) -> int:
        """A wall-clock budget that scales with how much work the plan actually contains.

        `job_timeout` alone is a flat ceiling, and a flat ceiling is wrong in both
        directions: too small and a legitimate larger plan dies with no defect to find, too
        large and a wedged job wastes two hours. The cost of a build is dominated by its
        **waves**, because tasks inside a wave run concurrently, so the estimate is
        waves x worker budget plus one verification pass.

        The configured `job_timeout` stays a floor, so an operator who raised it still gets
        their number, and the estimate is capped by `max_job_timeout` so a pathological plan
        (a hundred chained tasks) cannot claim an unbounded run.

        With no plan the flat value is returned, which is what a plan-only job needs.
        """
        if plan is None:
            return self.job_timeout
        waves = plan.waves()
        if not waves:
            return self.job_timeout
        concurrency = max(1, self.worker_concurrency)
        # A wave with more tasks than the concurrency allows runs in ceil(n/concurrency)
        # sequential batches, so it costs more than one worker budget.
        worker_batches = sum(-(-len(wave) // concurrency) for wave in waves)
        verify = profile.timeout if profile is not None else 0
        repair = self.max_repairs * (profile.timeout if profile is not None else 0)
        estimate = worker_batches * self.worker_timeout + verify + repair
        return max(self.job_timeout, min(estimate, self.max_job_timeout))

    def validate(self):
        if len(self.operator_token) < 32 or len(self.release_token) < 32:
            raise ValueError("Set two distinct FACTORY_*_TOKEN secrets, each at least 32 characters")
        if self.operator_token == self.release_token:
            raise ValueError("Operator and release tokens must be distinct")
        if not 0 < self.max_cost <= 100 or not 10 <= self.agent_timeout <= 7200:
            raise ValueError("Invalid agent cost/time budget")
        if not 60 <= self.job_timeout <= 86400:
            raise ValueError("Job timeout must be between 60s and 24h")
        if not self.job_timeout <= self.max_job_timeout <= 172800:
            raise ValueError("Max job timeout must be at least the job timeout and at most 48h")
        if not 30 <= self.worker_timeout <= 14400:
            raise ValueError("Worker timeout must be between 30s and 4h")
        if not 1 <= self.worker_concurrency <= 16:
            raise ValueError("Worker concurrency must be between 1 and 16")
        if not self.max_cost <= self.max_job_cost <= 1000:
            raise ValueError("Job cost budget must cover at least one model invocation")
        self.data.mkdir(parents=True, exist_ok=True, mode=0o700)

