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
