"""Serve a verified build output on a local port.

Not a build and not a deploy to another host. The verifier already ran `npm ci` and
`npm run build` inside a temporary tree and copied the result to `data/jobs/<job>/output/`;
this takes a copy of that output and keeps a Node process running so a Cloudflare hostname can
point at it.

Two facts shape the code:

- **A copy is mandatory, not tidiness.** `Engine.verify` deletes `output/` before the next
  verification of the same project, so an app served straight from there dies — or dangles, if
  symlinked — the moment someone rebuilds. The copy owns its own lifecycle instead.
- **A served app is one process for both halves.** The generated product is Nuxt SSR: `/` is
  rendered from `server/chunks/`, `/api/*` is handled from the same bundle, and `/_nuxt/*` is
  static from `public/`. There is no separate frontend to deploy and no second port.

One version is kept and there is no rollback archive. A bad rebuild replaces the copy; the
acceptance suite ran before that output was published, and it is the guard.
"""
import shutil
from pathlib import Path

from .local_app import LocalApp, app_data_dir, expand_env, local_env


class ServeError(RuntimeError):
    """A refusal an operator can act on, rather than a traceback."""


def servable(profile) -> tuple[bool, str]:
    """Whether a profile can be served, and why not.

    Both fields are required and they are not interchangeable: `artifact_dir` says what the
    build produced, `serve_argv` says how to start it. A profile with only the first has
    output nobody knows how to run, and returning that as a crash teaches the operator nothing.
    """
    missing = []
    if not getattr(profile, "artifact_dir", ""):
        missing.append("artifact_dir")
    if not getattr(profile, "serve_argv", None):
        missing.append("serve_argv")
    if missing:
        return False, ("This profile cannot be served because it does not define "
                       + " or ".join(missing))
    return True, ""


def output_dir(settings, job_id: str) -> Path:
    return Path(settings.data) / "jobs" / job_id / "output"


def served_dir(settings, project: str) -> Path:
    return Path(settings.data) / "served" / project


def stage(settings, project: str, job_id: str, profile) -> Path:
    """Copy a published build output into the directory that owns a served app.

    Copies rather than symlinks because the source is deleted on the next verification, and
    copies the whole directory because a Nitro build is self-contained: its runtime
    dependencies live in `server/node_modules` inside the output, so nothing beside it is
    needed. Measured outputs are 3-30 MB.
    """
    source = output_dir(settings, job_id)
    if not source.is_dir():
        raise ServeError(
            "This build has no output directory to serve. The artifact is the source tree; "
            "serving needs the result of the build, which is kept at data/jobs/<job>/output/.")
    target = served_dir(settings, project)
    if target.exists():
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target, symlinks=True)
    return target


def serve_argv_in(profile, tree: Path) -> list[str]:
    """Rewrite the profile's `serve_argv` to be relative to the served tree.

    `serve_argv` is written against the *repository*, not against the build output:
    `node release/server/index.mjs`, where `release` is `profile.artifact_dir`. The served
    tree is the *contents* of that directory, so running the argv as written looks for
    `release/release/server/index.mjs` and the process exits before readiness — which is
    exactly how the first live attempt failed.

    Only a leading `artifact_dir/` is stripped, and only when the resulting path exists. A
    profile whose argv already points at the tree is left alone, so this cannot mangle an
    argv that was correct.
    """
    prefix = getattr(profile, "artifact_dir", "").strip("/")
    argv = list(profile.serve_argv)
    if not prefix:
        return argv
    rewritten = []
    for arg in argv:
        candidate = arg
        if arg == prefix:
            candidate = "."
        elif arg.startswith(prefix + "/"):
            candidate = arg[len(prefix) + 1:]
        if candidate != arg and not (tree / candidate).exists():
            # The rewrite would point at something that is not there. Only the *script* has to
            # resolve; flags and values are not paths. Keep what was written so a genuine
            # failure reports the real path rather than this translation.
            if candidate.endswith((".mjs", ".js", ".cjs")):
                candidate = arg
        rewritten.append(candidate)
    return rewritten


def start(settings, project: str, job_id: str, profile, cancelled=lambda: False) -> LocalApp:
    """Start a served app and return the handle that keeps it alive.

    The caller owns the returned object: this module does not keep a registry in memory, so a
    process started here is only alive because somebody holds the reference.
    """
    ok, reason = servable(profile)
    if not ok:
        raise ServeError(reason)
    tree = stage(settings, project, job_id, profile)
    env = local_env()
    data_dir = app_data_dir(settings, project)
    if profile.env:
        env.update(expand_env(profile.env, data_dir=data_dir, app_dir=tree))
    # The served app reads its database from the persistent per-product directory, never from
    # the served copy: a rebuild replaces that copy, and data must outlive it.
    env.setdefault("DATA_DIR", str(data_dir))
    app = LocalApp(serve_argv_in(profile, tree), tree, env)
    try:
        app.wait_ready(profile.readiness_path, timeout=90, cancelled=cancelled)
    except BaseException:
        app.close()
        raise
    return app


def stop(app: LocalApp):
    """Stop the process group and leave nothing behind."""
    app.close()
