import json
import shutil

import pytest
from fastapi.testclient import TestClient

from factory.api import create_app
from factory.engine import Engine
from factory.models import Change, Profile, Review

pytestmark = pytest.mark.skipif(not shutil.which('npm'), reason='Node/npm required')


class NodeAgent:
    def __init__(self, broken=False):
        self.broken = broken
        self.sources = []

    def generate(self, role, instruction, context, schema, cancelled):
        if role == 'review':
            return Review(approved=True, findings=[])
        self.sources.append(context['files'])
        package = {'name': 'integration-fixture', 'version': '1.0.0', 'private': True,
                   'scripts': {'test:integration': 'node --test integration.cjs', 'build': 'node build.cjs'}}
        return Change(summary='Create testable local Node application', files={
            'package.json': json.dumps(package),
            'package-lock.json': json.dumps({'name':package['name'], 'version':'1.0.0', 'lockfileVersion':3,
                                            'packages':{'':{'name':package['name'],'version':'1.0.0'}}}),
            'server.cjs': "module.exports = require('node:http').createServer((req,res)=>{res.setHeader('Content-Type','application/json');res.end(JSON.stringify({ok:true}));});",
            'integration.cjs': "const {test}=require('node:test');const assert=require('node:assert/strict');const server=require('./server.cjs');test('HTTP JSON contract',async()=>{await new Promise(r=>server.listen(0,'127.0.0.1',r));try{const response=await fetch('http://127.0.0.1:'+server.address().port);assert.equal(response.status,200);assert.deepEqual(await response.json(),{ok:" + ('false' if self.broken else 'true') + "});}finally{await new Promise(r=>server.close(r));}});",
            'build.cjs': "const fs=require('node:fs');fs.mkdirSync('dist',{recursive:true});fs.writeFileSync('dist/index.html','<title>Verified build</title>');"
        })


def prepare(settings, monkeypatch):
    monkeypatch.setenv('FACTORY_ALLOW_NATIVE', '1')
    settings.profiles['static'] = Profile(runner='native', commands=[['npm','ci','--ignore-scripts','--no-audit','--no-fund'], ['npm','run','test:integration'], ['npm','run','build']], timeout=60)


def test_native_api_build_edit_rebuild(settings, store, project, plan, monkeypatch):
    prepare(settings, monkeypatch)
    agent = NodeAgent()
    engine = Engine(settings, store, agent=agent)
    with TestClient(create_app(settings)) as client:
        client.headers['Authorization'] = 'Bearer ' + settings.operator_token
        base = '/api/projects/' + project['id']
        assert client.post(base + '/approve?revision=1').status_code == 202
        first_job = store.claim()
        engine.process(first_job)
        first = client.get(base).json()
        assert first['state'] == 'ready'
        original = (settings.data / 'jobs' / first_job['id'] / 'source' / 'index.html')
        assert not original.exists()
        plan.tasks[0].instructions = 'Revise the completed application and retain its existing HTTP contract.'
        assert client.put(base + '/plan', json={'revision':1,'plan':plan.model_dump()}).status_code == 200
        assert client.post(base + '/approve?revision=1').status_code == 409
        assert client.post(base + '/approve?revision=2').status_code == 202
        second_job = store.claim()
        engine.process(second_job)
        second = client.get(base).json()
        assert second['state'] == 'ready'
        assert json.loads(second['artifact'])['revision'] == 2
        assert 'server.cjs' in agent.sources[1]
        assert (settings.data / 'jobs' / first_job['id'] / 'artifact.tar.gz').exists()
        log = (settings.data / 'jobs' / second_job['id'] / 'verification.txt').read_text()
        assert 'HTTP JSON contract' in log and 'STEP 3 PASS' in log


def test_failed_integration_blocks_build(settings, store, project, monkeypatch):
    prepare(settings, monkeypatch)
    store.enqueue(project['id'], 'build', {'revision':1}, next_state='building')
    job = store.claim()
    Engine(settings, store, agent=NodeAgent(broken=True)).process(job)
    assert store.project(project['id'])['state'] == 'failed'
    assert store.project(project['id'])['artifact'] is None
    assert not (settings.data / 'jobs' / job['id'] / 'artifact.tar.gz').exists()


def test_native_requires_explicit_opt_in(settings, tmp_path, monkeypatch):
    from factory.verifier import NativeVerifier
    monkeypatch.delenv('FACTORY_ALLOW_NATIVE', raising=False)
    with pytest.raises(RuntimeError, match='FACTORY_ALLOW_NATIVE'):
        NativeVerifier().verify(Profile(runner='native', commands=[['npm','run','build']]), tmp_path, tmp_path/'out', 'test')
