import json
import time

import pytest
from fastapi.testclient import TestClient

from factory.api import create_app
from factory.db import Conflict
from factory.studio import Studio
from factory.models import Profile


def draft(settings, store, project):
    settings.profiles['static'] = Profile(runner='native',commands=[['true']],studio=True)
    original=settings.data/'jobs'/'baseline'/'source'
    original.mkdir(parents=True)
    (original/'content').mkdir()
    (original/'content'/'site.json').write_text('{"title":"Before"}')
    (original/'app.vue').write_text('<template>Unchanged logic</template>')
    edited=settings.data/'studio'/'draft'/'source'
    (edited/'content').mkdir(parents=True)
    (edited/'content'/'site.json').write_text('{"title":"After"}')
    (edited/'app.vue').write_text('Must not import arbitrary code changes')
    with store.tx() as db:
        db.execute("UPDATE projects SET state='ready',artifact=? WHERE id=?",(json.dumps({'job_id':'baseline','digest':'a'*64}),project['id']))
        db.execute('INSERT INTO studio_drafts VALUES(?,?,?,?,?,?)',('draft',project['id'],'baseline',1,'stopped',time.time()))
    return Studio(settings,store),original,edited


def test_apply_draft_creates_revision_without_changing_verified_source(settings,store,project,monkeypatch):
    monkeypatch.setenv('FACTORY_ALLOW_NATIVE','1')
    studio,original,edited=draft(settings,store,project)
    assert len(studio.diff(project['id'])['files'])==1
    job=studio.apply(project['id'])
    assert job['kind']=='verify'
    row=store.project(project['id'])
    assert row['revision']==2
    imported=settings.data/'edits'/row['pending_edit']/'source'
    assert json.loads((imported/'content'/'site.json').read_text())['title']=='After'
    assert (original/'content'/'site.json').read_text()=='{"title":"Before"}'
    assert (imported/'app.vue').read_text()=='<template>Unchanged logic</template>'
    assert json.loads(row['artifact'])['job_id']=='baseline'
    with pytest.raises(Conflict):
        studio.apply(project['id'])


def test_stale_draft_rejected(settings,store,project,monkeypatch):
    monkeypatch.setenv('FACTORY_ALLOW_NATIVE','1')
    studio,_,_=draft(settings,store,project)
    with store.tx() as db:
        db.execute('UPDATE projects SET revision=2 WHERE id=?',(project['id'],))
    with pytest.raises(Conflict,match='changed'):
        studio.apply(project['id'])


def test_content_symlink_rejected(settings,store,project):
    studio,_,edited=draft(settings,store,project)
    (edited/'content'/'escape.md').symlink_to('/etc/passwd')
    with pytest.raises(ValueError):
        studio.diff(project['id'])


def test_remote_studio_mutation_denied(settings,store,project):
    draft(settings,store,project)
    with TestClient(create_app(settings),client=('192.0.2.1',123)) as client:
        response=client.post('/api/projects/'+project['id']+'/studio/start',headers={'Authorization':'Bearer '+settings.operator_token})
        assert response.status_code==403


def test_restart_preserves_draft_files(settings,store,project):
    _,_,edited=draft(settings,store,project)
    with store.tx() as db:
        db.execute("UPDATE studio_drafts SET state='editing'")
    restored=Studio(settings,store)
    assert restored.state(project['id'])['state']=='stopped'
    assert json.loads((edited/'content'/'site.json').read_text())['title']=='After'


def test_studio_edit_reaches_the_product_and_is_verified(settings, store, project, monkeypatch):
    """A reviewed Studio draft must actually be published, not silently dropped.

    Regression guard: the engine once stopped reading `pending_edit`, so an applied edit
    produced a revision that never changed the product source.
    """
    from factory.engine import Engine
    from factory.models import Plan, Review

    monkeypatch.setenv("FACTORY_ALLOW_NATIVE", "1")
    # The verifier requires a real artifact directory, so the "build" creates one.
    settings.profiles["static"] = Profile(
        runner="native", studio=True, artifact_dir="dist",
        commands=[["python3", "-c", "import pathlib;pathlib.Path('dist').mkdir(exist_ok=True);"
                                    "pathlib.Path('dist/index.html').write_text('<title>Built</title>')"]])

    class Agent:
        def generate(self, role, instruction, context, schema, cancelled):
            assert role == "review", role
            return Review(approved=True, findings=[])

        def run_task(self, worktree, instruction, model="", timeout=0, cancelled=lambda: False):
            (worktree / "app.vue").write_text("<template>built</template>")
            (worktree / "content").mkdir(exist_ok=True)
            (worktree / "content" / "site.json").write_text('{"title":"Before"}')
            return "scripted worker"

    plan = Plan(summary="Build the site that Studio will edit afterwards.",
                tasks=[{"id": "site", "title": "Build site", "instructions": "Create the site files.",
                        "acceptance": ["renders"], "depends_on": []}])
    with store.tx() as db:
        db.execute("UPDATE projects SET plan=?,revision=1,state='awaiting_plan' WHERE id=?",
                   (plan.model_dump_json(), project["id"]))

    agent = Agent()
    engine = Engine(settings, store, agent=agent, worker=agent)

    # First build creates the product repository and its content.
    store.enqueue(project["id"], "build", {"revision": 1}, next_state="building")
    engine.process(store.claim())
    assert store.project(project["id"])["state"] == "ready"

    # A reviewed draft changes only content, exactly as Studio allows.
    edit = settings.data / "edits" / "e1" / "source"
    (edit / "content").mkdir(parents=True)
    (edit / "content" / "site.json").write_text('{"title":"After"}')
    (edit / "app.vue").write_text("<template>built</template>")
    with store.tx() as db:
        db.execute("UPDATE projects SET pending_edit=?,revision=2,state='building' WHERE id=?",
                   ("e1", project["id"]))
        db.execute("INSERT INTO jobs(id,project_id,kind,payload,created) VALUES(?,?,?,?,?)",
                   ("job-studio", project["id"], "verify", json.dumps({"revision": 2}), time.time()))

    engine.process(store.claim())

    row = store.project(project["id"])
    assert row["state"] == "ready", "the imported edit must verify"
    assert row["pending_edit"] is None, "a verified edit is consumed"
    product = settings.data / "products" / project["id"] / "content" / "site.json"
    assert json.loads(product.read_text())["title"] == "After", "the edit must reach the product branch"
    kinds = [e["kind"] for e in store.all("SELECT kind FROM events WHERE project_id=?", (project["id"],))]
    assert "studio.imported" in kinds


def test_starter_journey_lands_content_in_the_product(settings, store, project, monkeypatch):
    """The documented starter journey: create starter -> verify -> product holds the content.

    This is the editor path the operator actually uses, so it must be covered end to end
    rather than only at the draft level.
    """
    from factory.engine import Engine

    monkeypatch.setenv("FACTORY_ALLOW_NATIVE", "1")
    settings.profiles["nuxt-studio"] = Profile(
        runner="native", studio=True, artifact_dir="dist",
        commands=[["python3", "-c", "import pathlib;pathlib.Path('dist').mkdir(exist_ok=True);"
                                    "pathlib.Path('dist/index.html').write_text('<title>Built</title>')"]])
    from factory.models import ProjectInput
    # A starter may only initialise a brand-new project, so create one for this journey.
    fresh = store.create_project(ProjectInput(name="Starter journey", brief="Create an editable site with Studio content.",
                                              profile="nuxt-studio"))
    project = fresh
    studio = Studio(settings, store)
    studio.starter(project["id"])
    draft = store.project(project["id"])["pending_edit"]
    assert draft, "creating a starter must open a draft, not publish directly"
    assert (settings.data / "edits" / draft / "source" / "content" / "site.json").is_file()

    Engine(settings, store).process(store.claim())

    row = store.project(project["id"])
    assert row["state"] == "ready"
    assert row["artifact"]
    product = settings.data / "products" / project["id"]
    assert (product / "content" / "site.json").is_file(), "starter content must reach the product branch"
    assert (product / "nuxt.config.ts").is_file()
