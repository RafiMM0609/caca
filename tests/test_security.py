import io
import sys
import tarfile

import pytest

from factory.models import Plan, Profile, safe_path
from factory.process import Cancelled, ProcessError, run
from factory.workspace import extract, package


@pytest.mark.parametrize("name", ["../escape", "/etc/passwd", "a/../../x", "a\\b", ".git/config", ".pi/config.yml", ".env", "a/.env.production", "./a", "a//b", "a\nname", ".", ""])
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


def test_a_docker_profile_is_rejected_with_a_clear_message():
    """Docker support was removed; an image-based profile must fail loudly, not be reinterpreted.

    The runner field keeps a single legal value, so a config that still says "docker" gets an
    explicit error naming the field instead of silently running natively with an unused image.
    """
    with pytest.raises(ValueError):
        Profile(runner="docker", commands=[["true"]], artifact_dir="x")
    with pytest.raises(ValueError):
        Profile(image="python:latest", verify_argv=["true"], checks_dir="/checks")


def test_a_native_profile_that_declares_an_image_is_rejected():
    """`image`/`verify_argv` are dead fields now, so supplying them is a configuration mistake."""
    with pytest.raises(ValueError):
        Profile(runner="native", commands=[["true"]], artifact_dir="x", image="python@sha256:" + "a" * 64)


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "parent", "duplicate", "oversize"])
def test_reject_malicious_tar(tmp_path, kind):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        member = tarfile.TarInfo("../outside" if kind == "parent" else "file")
        if kind in ("symlink", "hardlink"):
            member.type = tarfile.SYMTYPE if kind == "symlink" else tarfile.LNKTYPE
            member.linkname = "/etc/passwd"
        if kind == "oversize":
            # Follow the real ceiling rather than a hardcoded number: the limit moved from
            # 20 MB to MAX_ARTIFACT when the artifact became the source tree.
            from factory.workspace import MAX_ARTIFACT
            member.size = MAX_ARTIFACT + 1
            archive.addfile(member, io.BytesIO(b"x" * member.size))
        else:
            archive.addfile(member)
        if kind == "duplicate":
            archive.addfile(member)
    with pytest.raises(ValueError):
        extract(output.getvalue(), tmp_path)


def test_packaging_accepts_internal_symlinks_but_rejects_escaping_ones(tmp_path):
    """A build legitimately links inside its tree; it must never link outside it."""
    root = tmp_path / "app"
    (root / "shared").mkdir(parents=True)
    (root / "shared" / "lib.js").write_text("module.exports = 1;\n")
    (root / "link").symlink_to("shared/lib.js")
    record = package(root, tmp_path / "artifact.tar.gz")
    assert record["files"] == 2

    escape = tmp_path / "escape"
    escape.mkdir()
    (escape / "leak").symlink_to("/etc/passwd")
    with pytest.raises(ValueError, match="must be relative"):
        package(escape, tmp_path / "escape.tar.gz")


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



def test_build_tree_clone_preserves_symlinks_but_source_clone_rejects_them(tmp_path):
    """A build tree legitimately contains symlinks; generated *source* must not.

    `node_modules/.bin/*` is entirely symlinks, so the per-case copy of a real Node build
    tree cannot use the strict source walk without failing every Nuxt verification. The
    strict walk must stay strict for source, which is the path that moves generated code.
    """
    from factory.workspace import clone_build_tree, clone_tree

    build = tmp_path / "built"
    (build / "node_modules" / ".bin").mkdir(parents=True)
    (build / "node_modules" / "pkg").mkdir(parents=True)
    (build / "node_modules" / "pkg" / "cli.js").write_text("#!/usr/bin/env node\n")
    (build / "node_modules" / ".bin" / "cli").symlink_to("../pkg/cli.js")

    case_tree = tmp_path / "case-01-tree"
    clone_build_tree(build, case_tree)
    link = case_tree / "node_modules" / ".bin" / "cli"
    assert link.is_symlink(), "symlinks must be preserved, not dereferenced"
    assert link.read_text() == "#!/usr/bin/env node\n"

    # The strict source clone must keep rejecting symlinks.
    with pytest.raises(ValueError, match="Symlinks are not allowed"):
        clone_tree(build, tmp_path / "strict")
