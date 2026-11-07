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
    omp: str = "omp"
    model: str = ""
    review_model: str = ""
    tester_model: str = ""
    max_cost: float = 2.0
    max_job_cost: float = 20.0
    agent_timeout: int = 600
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
            omp=os.environ.get("FACTORY_OMP", "omp"),
            model=os.environ.get("FACTORY_MODEL", ""),
            review_model=os.environ.get("FACTORY_REVIEW_MODEL", ""),
            tester_model=os.environ.get("FACTORY_TESTER_MODEL", ""),
            max_cost=float(os.environ.get("FACTORY_MAX_COST", "2")),
            max_job_cost=float(os.environ.get("FACTORY_MAX_JOB_COST", "20")),
            agent_timeout=int(os.environ.get("FACTORY_AGENT_TIMEOUT", "600")),
            memorix=os.environ.get("FACTORY_MEMORIX", ""),
            worker_concurrency=int(os.environ.get("FACTORY_WORKER_CONCURRENCY", "3")),
            profiles={k: Profile.model_validate(v) for k, v in raw.get("profiles", {}).items()},
            targets={k: Target.model_validate(v) for k, v in raw.get("targets", {}).items()},
        )

    def validate(self):
        if len(self.operator_token) < 32 or len(self.release_token) < 32:
            raise ValueError("Set two distinct FACTORY_*_TOKEN secrets, each at least 32 characters")
        if self.operator_token == self.release_token:
            raise ValueError("Operator and release tokens must be distinct")
        if not 0 < self.max_cost <= 100 or not 10 <= self.agent_timeout <= 7200:
            raise ValueError("Invalid agent cost/time budget")
        if not 1 <= self.worker_concurrency <= 16:
            raise ValueError("Worker concurrency must be between 1 and 16")
        if not self.max_cost <= self.max_job_cost <= 1000:
            raise ValueError("Job cost budget must cover at least one model invocation")
        self.data.mkdir(parents=True, exist_ok=True, mode=0o700)

