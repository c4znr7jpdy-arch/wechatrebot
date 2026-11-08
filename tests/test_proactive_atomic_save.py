"""Save real session files under serialization, disk and cancellation faults."""
import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
import tempfile
import threading
from types import MethodType, SimpleNamespace as NS
from unittest.mock import Mock

import pytest

from test_hot_config_runtime import _runtime_function
from test_plugin_portability import _patched


PLUGIN = "astrbot_plugin_proactive_chat"
ROOT = Path(__file__).parents[1]
OLD = {"wechat:FriendMessage:contact": {"unanswered_count": 1, "next_trigger_time": 1800000000}}
NEW = {"wechat:FriendMessage:contact": {"unanswered_count": 2, "next_trigger_time": 1800005000,
                                          "recent_proactive_messages": ["你好"]}}


@pytest.fixture
def storage(tmp_path):
    destination = tmp_path / "core/data_storage.py"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes((ROOT / "vendor/data/plugins" / PLUGIN / "core/data_storage.py").read_bytes())
    source = _patched(tmp_path, PLUGIN, "core/data_storage.py", all_patches=True)
    # No robot dependencies are loaded. The async adapter uses actual files;
    # only the scheduling boundary is replaced so the pre-fix method also runs.
    @asynccontextmanager
    async def open_file(path, *args, **kwargs):
        with Path(path).open(*args, **kwargs) as handle:
            async def write(content):
                return handle.write(content)
            yield NS(write=write)

    async def makedirs(path, **kwargs):
        Path(path).mkdir(**kwargs)

    scope = {"asyncio": asyncio, "json": json, "tempfile": tempfile, "Path": Path,
             "logger": Mock(), "aio_os": NS(makedirs=makedirs), "aiofiles": NS(open=open_file)}
    owner = NS(data_dir=tmp_path / "state", session_data_file=tmp_path / "state/session_data.json",
               session_data=NEW, data_lock=asyncio.Lock())
    owner.data_dir.mkdir()
    owner.session_data_file.write_text(json.dumps(OLD), encoding="utf-8")
    owner._save_data_internal = MethodType(_runtime_function(source, "_save_data_internal", scope), owner)
    return owner, scope


def test_normal_save_keeps_json_format_unicode_and_no_temporary_files(storage):
    owner, _scope = storage
    asyncio.run(owner._save_data_internal())
    assert json.loads(owner.session_data_file.read_text("utf-8")) == NEW
    assert "你好" in owner.session_data_file.read_text("utf-8")
    assert sorted(path.name for path in owner.data_dir.iterdir()) == ["session_data.json"]


@pytest.mark.parametrize("fault", ["serialization", "disk-write", "replace"])
def test_failed_save_preserves_previous_recoverable_state(storage, monkeypatch, fault):
    owner, scope = storage
    before = owner.session_data_file.read_bytes()
    if fault == "serialization":
        owner.session_data = {"invalid": object()}
    elif fault == "disk-write":
        def dump(*args, **kwargs):
            if len(args) > 1:
                args[1].write('{"partial":')
            raise OSError("test full disk")
        scope["json"] = NS(dump=dump, dumps=dump)
    else:
        original = Path.replace

        def replace(path, target):
            if Path(target) == owner.session_data_file:
                raise OSError("test destination locked")
            return original(path, target)

        monkeypatch.setattr(Path, "replace", replace)
    if fault == "serialization":
        with pytest.raises(TypeError):
            asyncio.run(owner._save_data_internal())
    else:
        asyncio.run(owner._save_data_internal())
    assert owner.session_data_file.read_bytes() == before
    assert sorted(path.name for path in owner.data_dir.iterdir()) == ["session_data.json"]


def test_cancelling_save_waits_for_atomic_commit_before_releasing_caller_lock(storage):
    owner, scope = storage
    started, release = threading.Event(), threading.Event()

    def paused_dump(*args, **kwargs):
        started.set()
        if not release.wait(5):
            raise TimeoutError("test serializer was not released")
        return json.dump(*args, **kwargs) if len(args) > 1 else json.dumps(*args, **kwargs)

    scope["json"] = NS(dump=paused_dump, dumps=paused_dump)

    async def run():
        async def save():
            async with owner.data_lock:
                await owner._save_data_internal()

        task = asyncio.create_task(save())
        try:
            assert await asyncio.to_thread(started.wait, 5)
            task.cancel()
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert not task.done()
            assert owner.data_lock.locked()
            assert json.loads(owner.session_data_file.read_text("utf-8")) == OLD
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
        assert task.cancelled()
        assert not owner.data_lock.locked()

    asyncio.run(run())
    assert json.loads(owner.session_data_file.read_text("utf-8")) == NEW
    assert sorted(path.name for path in owner.data_dir.iterdir()) == ["session_data.json"]
