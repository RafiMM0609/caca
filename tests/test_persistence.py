"""A generated application must be able to keep data across restarts.

Before this existed, a profile could not hand an application any environment at all, and the
only directory the app could write to was deleted with the disposable build tree. A CRUD app
therefore could not pass "create a record, then read it back" even once.
"""

import pytest

from factory.local_app import app_data_dir, expand_env
from factory.models import Profile

pytestmark = pytest.mark.skipif(not __import__("shutil").which("node"), reason="Node required")


def test_profile_env_cannot_smuggle_a_credential_to_a_generated_app():
    """The generated app is the least trusted process; it must never receive a factory key."""
    for name in ("FACTORY_OPERATOR_TOKEN", "OPENCODE_API_KEY", "FACTORY_MODEL"):
        with pytest.raises(ValueError, match="reserved"):
            Profile(runner="native", commands=[["true"]], env={name: "x"})
    # Lowercase keys are rejected outright so a shell cannot be confused about case.
    with pytest.raises(ValueError):
        Profile(runner="native", commands=[["true"]], env={"database_url": "x"})


def test_profile_env_only_expands_known_placeholders(tmp_path):
    with pytest.raises(ValueError, match="Unknown placeholder"):
        Profile(runner="native", commands=[["true"]], env={"DB": "file:{home}/x.db"})
    profile = Profile(runner="native", commands=[["true"]],
                      env={"DATABASE_URL": "file:{data}/app.db", "DATA_DIR": "{data}"})
    expanded = expand_env(profile.env, data_dir=tmp_path)
    assert expanded["DATABASE_URL"] == f"file:{tmp_path}/app.db"
    assert expanded["DATA_DIR"] == str(tmp_path)


def test_data_dir_lives_beside_the_repo_not_inside_it(tmp_path):
    """A database committed into the product repo would ship in the artifact and be clobbered."""
    class Settings:
        data = tmp_path

    path = app_data_dir(Settings, "project-1")
    assert path == tmp_path / "products" / "project-1" / "data"
    assert path.is_dir()
    assert ".git" not in path.parts


SERVER = '''
const http = require("node:http");
const fs = require("node:fs");
const path = require("node:path");
const file = (process.env.DATABASE_URL || "file:./fallback.db").replace(/^file:/, "");
const store = { items: [] };
if (fs.existsSync(file)) Object.assign(store, JSON.parse(fs.readFileSync(file, "utf8")));
const save = () => {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, JSON.stringify(store));
};
http.createServer((req, res) => {
  if (req.url === "/api/health") { res.end(JSON.stringify({ status: "ok" })); return; }
  if (req.url === "/api/items" && req.method === "POST") {
    let body = "";
    req.on("data", chunk => body += chunk);
    req.on("end", () => {
      const item = JSON.parse(body || "{}");
      store.items.push(item);
      save();
      res.statusCode = 201;
      res.setHeader("content-type", "application/json");
      res.end(JSON.stringify({ ok: true, count: store.items.length }));
    });
    return;
  }
  if (req.url === "/api/items") {
    res.setHeader("content-type", "application/json");
    res.end(JSON.stringify({ items: store.items }));
    return;
  }
  res.statusCode = 404; res.end("no");
}).listen(Number(process.env.PORT || 0), "127.0.0.1");
'''


def _serve(app_dir, data_dir, port):
    from factory.local_app import expand_env, local_env
    env = local_env()
    env.update(expand_env({"DATABASE_URL": "file:{data}/app.db"}, data_dir=data_dir))
    env["PORT"] = str(port)
    import subprocess
    return subprocess.Popen(["node", "server.cjs"], cwd=app_dir, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)


def test_records_survive_a_restart(tmp_path):
    """The whole point of persistence: write, stop the process, start again, still there."""
    import httpx
    import socket
    import time

    app_dir = tmp_path / "app"
    app_dir.mkdir()
    (app_dir / "server.cjs").write_text(SERVER)
    data_dir = tmp_path / "products" / "p" / "data"
    data_dir.mkdir(parents=True)

    def free_port():
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]

    def wait(port):
        with httpx.Client(trust_env=False, timeout=2) as client:
            for _ in range(100):
                try:
                    if client.get(f"http://127.0.0.1:{port}/api/health").status_code == 200:
                        return
                except httpx.HTTPError:
                    time.sleep(0.1)
        raise AssertionError("server never became ready")

    port = free_port()
    process = _serve(app_dir, data_dir, port)
    try:
        wait(port)
        with httpx.Client(trust_env=False, timeout=5) as client:
            created = client.post(f"http://127.0.0.1:{port}/api/items", json={"name": "teak table"})
            assert created.status_code == 201
            assert created.json()["count"] == 1
    finally:
        process.terminate()
        process.wait(timeout=10)

    assert (data_dir / "app.db").is_file(), "the database must live in the persistent data dir"

    port = free_port()
    process = _serve(app_dir, data_dir, port)
    try:
        wait(port)
        with httpx.Client(trust_env=False, timeout=5) as client:
            items = client.get(f"http://127.0.0.1:{port}/api/items").json()["items"]
        assert items == [{"name": "teak table"}], "data written before the restart must still be there"
    finally:
        process.terminate()
        process.wait(timeout=10)


def test_serve_argv_is_relative_to_the_artifact_root(tmp_path):
    """The artifact root IS artifact_dir, so a leading `release/` must be stripped.

    `factory run` joined serve_argv against the job output and asked node for
    release/release/server/index.mjs, so a published project could not be started.
    """

    source = tmp_path / "output"
    (source / "server").mkdir(parents=True)
    (source / "server" / "index.mjs").write_text("/* bundle */")
    profile = Profile(runner="native", commands=[["true"]], artifact_dir="release",
                      serve_argv=["node", "release/server/index.mjs"])
    argv = list(profile.serve_argv)
    prefix = profile.artifact_dir.strip("/") + "/"
    if argv[-1].startswith(prefix):
        argv[-1] = argv[-1][len(prefix):]
    assert (source / argv[-1]).is_file(), "serve_argv must resolve inside the artifact root"
