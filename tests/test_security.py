import io
import sys
import tarfile

import pytest

from factory.models import Plan, Profile, safe_path
from factory.process import Cancelled, ProcessError, run
from factory.workspace import extract, package


@pytest.mark.parametrize("name", ["../escape", "/etc/passwd", "a/../../x", "a\\b", ".git/config", ".omp/config.yml", ".env", "a/.env.production", "./a", "a//b", "a\nname", ".", ""])
def test_reject_unsafe_generated_paths(name):
    with pytest.raises(ValueError):
        safe_path(name)


@pytest.mark.parametrize("name", [".env.example", ".env.sample", ".env.template", "apps/web/.env.example"])
def test_env_templates_are_allowed_but_never_real_secrets(name):
    """Blocking `.env.example` made every app that documents its variables unbuildable."""
    assert safe_path(name) == name


def test_reject_bad_dependency_order(plan):
    data = plan.model_dump()
    data["tasks"][0]["depends_on"] = ["missing"]
    with pytest.raises(ValueError):
        Plan.model_validate(data)


def test_moving_image_tag_forbidden():
    with pytest.raises(ValueError):
        Profile(image="python:latest", verify_argv=["true"], checks_dir="/checks")


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "parent", "duplicate", "oversize"])
def test_reject_malicious_tar(tmp_path, kind):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        member = tarfile.TarInfo("../outside" if kind == "parent" else "file")
        if kind in ("symlink", "hardlink"):
            member.type = tarfile.SYMTYPE if kind == "symlink" else tarfile.LNKTYPE
            member.linkname = "/etc/passwd"
        if kind == "oversize":
            member.size = 20_000_001
            archive.addfile(member, io.BytesIO(b"x" * member.size))
        else:
            archive.addfile(member)
        if kind == "duplicate":
            archive.addfile(member)
    with pytest.raises(ValueError):
        extract(output.getvalue(), tmp_path)


def test_packaging_rejects_symlinks(tmp_path):
    root = tmp_path / "app"
    root.mkdir()
    (root / "link").symlink_to("/etc/passwd")
    with pytest.raises(ValueError):
        package(root, tmp_path / "artifact.tar.gz")


def test_subprocess_timeout_output_limit_and_cancel():
    with pytest.raises(ProcessError, match="deadline"):
        run([sys.executable, "-c", "import time;time.sleep(10)"], timeout=0.1)
    with pytest.raises(Cancelled):
        run([sys.executable, "-c", "import time;time.sleep(10)"], cancelled=lambda: True)


def test_chatty_process_keeps_a_bounded_tail_instead_of_being_killed():
    """A tool-using worker streams far more than the limit; killing it destroyed real work.

    `npm ci` plus a Nuxt build easily exceeds a few megabytes of JSON events. The bound is
    on memory, so the run must succeed and keep the tail (which is what review reads).
    """
    text = run([sys.executable, "-c", "print('x'*10000);print('THE-END')"], max_output=100)
    assert "THE-END" in text, "the tail of the output must survive"
    assert "OUTPUT TRUNCATED" in text
    assert len(text) < 1000, "output stays bounded"



def test_subprocess_stdin_and_nonzero():
    assert run([sys.executable, "-c", "import sys;print(sys.stdin.read())"], input_data=b"hello").strip() == "hello"
    with pytest.raises(ProcessError, match="exit code 3"):
        run([sys.executable, "-c", "raise SystemExit(3)"])

