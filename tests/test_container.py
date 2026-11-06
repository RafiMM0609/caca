import os
import uuid

import pytest

from factory.engine import Engine
from factory.verifier import Verifier
from tests.test_workflow import ScriptedAgent, build_job


pytestmark = pytest.mark.skipif(os.environ.get("FACTORY_TEST_DOCKER") != "1", reason="Requires local Docker and pinned image")


def test_real_docker_verification_pipeline(settings, store, project, plan):
    build_job(store, project)
    job = store.claim()
    Engine(settings, store, ScriptedAgent(plan), Verifier()).process(job)
    assert store.project(project["id"])["state"] == "ready"
    evidence = settings.data / "jobs" / job["id"] / "verification.txt"
    assert "PASS: 1 HTML pages" in evidence.read_text()


def test_container_no_network_no_host_write(settings, tmp_path):
    source = tmp_path / "source"
    checks = tmp_path / "checks"
    source.mkdir()
    checks.mkdir()
    (source / "data.txt").write_text("original")
    (checks / "isolation.py").write_text('''import os, socket
from pathlib import Path
try:
    Path('/source/data.txt').write_text('modified')
    raise AssertionError('source writable')
except OSError:
    pass
try:
    Path('/etc/escape').write_text('modified')
    raise AssertionError('root writable')
except OSError:
    pass
assert not Path('/var/run/docker.sock').exists()
assert 'FACTORY_OPERATOR_TOKEN' not in os.environ
assert 'OPENAI_API_KEY' not in os.environ
s = socket.socket(); s.settimeout(1)
assert s.connect_ex(('1.1.1.1', 443)) != 0
Path('/output/ok.txt').write_text('isolation verified')
print('isolation passed')
''')
    profile = settings.profiles["static"].model_copy(update={"checks_dir": str(checks), "verify_argv": ["python3", "/checks/isolation.py"]})
    log = Verifier().verify(profile, source, tmp_path / "output", uuid.uuid4().hex)
    assert "isolation passed" in log
    assert (source / "data.txt").read_text() == "original"

