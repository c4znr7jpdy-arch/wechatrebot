"""Delivery contracts for the refreshed upstream plugins, without starting AstrBot."""
import asyncio
import hashlib
import json
from pathlib import Path
import threading
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest
from test_hot_config_runtime import _runtime_function

ROOT = Path(__file__).parents[1]
MEME = ROOT / 'runtime/meme_manager/mixins/web_api.py'


def test_native_sources_are_pinned_and_used_by_delivery():
    records = json.loads((ROOT/'release/native-plugin-sources.json').read_text('utf-8'))['plugins']
    manifest = json.loads((ROOT/'release/source-manifest.json').read_text('utf-8'))
    entries = manifest.get('entries', manifest.get('sources'))
    for record in records:
        base = ROOT/'runtime'/record['module']
        assert any(e['origin']=='commercial' and e['source']==f"runtime/{record['module']}" for e in entries)
        for name, digest in record['files'].items():
            assert hashlib.sha256((base/name).read_bytes()).hexdigest()==digest, name


@pytest.mark.parametrize('outcome', ['success', 'failure', 'cancel'])
def test_install_worker_leaves_event_loop_responsive_and_finishes(tmp_path, outcome):
    class Cancelled(Exception): pass
    entered,release=threading.Event(),threading.Event()
    threads=[]
    def install(**kwargs):
        threads.append(threading.get_ident()); entered.set()
        if not release.wait(2): raise RuntimeError('test release timeout')
        kwargs['progress_callback']('downloading',50,100)
        if outcome=='failure':raise ValueError('download failed')
        return {'pack_id':'example'}
    scope={'asyncio':asyncio,'PACKS_DIR':tmp_path,'install_pack_from_github_source':install,
           'InstallCancelledError':Cancelled,'time':__import__('time'),'logger':Mock()}
    source=MEME.read_text('utf-8')
    guarded=_runtime_function(source,'_run_guarded_runtime_file_operation',scope)
    runner=_runtime_function(source,'_run_community_pack_install_job',scope)
    job={'status':'running','progress':0,'cancel_requested':False}
    owner=NS(_community_install_jobs={'one':job},_semantic_operation_guard=None,
             _get_github_accelerator_url=lambda:'',_reload_personas=Mock())
    owner._run_guarded_runtime_file_operation=lambda *a,**kw:guarded(owner,*a,**kw)
    async def run():
        task=asyncio.create_task(runner(owner,'one',{},False,False))
        try:
            for _ in range(100):
                if entered.is_set():break
                await asyncio.sleep(.005)
            assert entered.is_set()
            # This executes while the installer is deliberately blocked in its worker.
            assert not task.done()
            assert job['status']=='running'
            if outcome=='cancel':job['cancel_requested']=True
        finally:
            release.set()
            await asyncio.wait_for(task,3)
    asyncio.run(run())
    assert threads[0]!=threading.get_ident()
    assert job['status']=={'success':'succeeded','failure':'failed','cancel':'cancelled'}[outcome]
    assert owner._reload_personas.call_count==(1 if outcome=='success' else 0)
