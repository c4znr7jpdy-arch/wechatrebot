import ast
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
from types import ModuleType, SimpleNamespace as NS

import pytest

ROOT = Path(__file__).parents[1]


def load_help(monkeypatch, tmp_path):
    # Mirror the installed command plugin: shared metadata code is shipped beside
    # command_help, so help works even when the capability gateway is disabled.
    folder = tmp_path / 'help_plugin'
    folder.mkdir()
    shutil.copyfile(ROOT / 'runtime/jiang_commands/command_help.py', folder / 'command_help.py')
    shutil.copyfile(ROOT / 'runtime/jiang_capability_router/declarations.py', folder / 'declarations.py')
    package = ModuleType('help_plugin')
    package.__path__ = [str(folder)]
    monkeypatch.setitem(sys.modules, 'help_plugin', package)
    monkeypatch.delitem(sys.modules, 'help_plugin.declarations', raising=False)
    spec = importlib.util.spec_from_file_location('help_plugin.command_help', folder / 'command_help.py')
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


def metadata(tmp_path, star, capabilities):
    folder = tmp_path / star.name
    folder.mkdir(exist_ok=True)
    (folder / 'capabilities.json').write_text(json.dumps({
        'schema_version': 1, 'capabilities': capabilities,
    }), encoding='utf-8')
    star.module = NS(__file__=str(folder / 'main.py'))
    star.module_path = star.name


def test_removal_patch_and_packaged_assets(tmp_path):
    source = ROOT / 'vendor/data/plugins/jiang_commands'
    target = tmp_path / 'commands'
    shutil.copytree(source, target)
    subprocess.run(['git', 'init', '--quiet'], cwd=tmp_path, check=True)
    for patch in sorted(ROOT.glob('patches/astrbot-plugin-jiang_commands*.patch')):
        subprocess.run(['git', 'apply', '--ignore-space-change', '--directory=commands', str(patch)],
                       cwd=tmp_path, check=True, capture_output=True)
    source = (target / 'main.py').read_text(encoding='utf-8')
    ast.parse(source)
    for marker in ('燕云', '理想', '塔罗', '占卜', '_TAROT', '_IDEAL', '_HELP_IMG', 'help_card'):
        assert marker not in source
    assert 'build_help(self.context, event)' in source
    patch = ROOT / 'patches/astrbot-plugin-jiang_commands--zz-command-cleanup.patch'
    subprocess.run(['git', 'apply', '--reverse', '--check', '--directory=commands', str(patch)],
                   cwd=tmp_path, check=True, capture_output=True)
    manifest = json.loads((ROOT / 'release/source-manifest.json').read_text(encoding='utf-8'))
    entries = {e['source']: e for e in manifest['entries']}
    assert {'tarot_cards', 'help_card.png', 'font_test_bold.png', 'font_test_normal.png'} <= set(entries['data/plugins/jiang_commands']['exclude'])
    assert {'help_card.py', 'plugin_manager.py'} <= set(entries['ai_plugin/ai_plugin']['exclude'])


def test_help_filters_disabled_plugins_permissions_and_refreshes(monkeypatch, tmp_path):
    module = load_help(monkeypatch, tmp_path)
    class Command:
        def __init__(self, *names): self.names = list(names)
        def get_complete_command_names(self): return self.names
        def custom_filter_ok(self, event, cfg): return True
    class Group(Command): pass
    class Permission:
        def filter(self, event, cfg): return event.is_admin()
    stars = [NS(root_dir_name=name, name=name, display_name=name, activated=True,
                reserved=False, star_cls=object()) for name in
             ['jiang_commands', 'astrbot_plugin_music', 'jiang_schedule', 'jiang_rocom']]
    metadata(tmp_path, stars[1], [{'id': 'song', 'description': '搜索并播放歌曲',
                                 'examples': ['/点歌 歌名']}])
    metadata(tmp_path, stars[2], [{'id': 'news', 'description': '定时发送新闻',
                                 'permission': 'group_admin', 'examples': ['/定时新闻 8点']}])
    owners = {s.name: s for s in stars}
    handlers = [NS(handler_module_path=name, desc='用途', event_filters=filters)
                for name, filters in [
                    ('jiang_commands', [Command('新闻')]),
                    ('astrbot_plugin_music', [Command('查歌词')]),
                    ('jiang_rocom', [Command('洛克档案')]),
                    ('jiang_commands', [Command('管理员测试'), Permission()])]]
    modules = {
        'astrbot.core.star.star': NS(star_map=owners),
        'astrbot.core.star.star_handler': NS(EventType=NS(AdapterMessageEvent=1),
            star_handlers_registry=NS(get_handlers_by_event_type=lambda *a, **k: handlers)),
        'astrbot.core.star.filter.command': NS(CommandFilter=Command),
        'astrbot.core.star.filter.command_group': NS(CommandGroupFilter=Group),
        'astrbot.core.star.filter.permission': NS(PermissionTypeFilter=Permission),
        'astrbot.core.platform.wechat_group_policy': NS(group_plugins=lambda *a, **k: []),
    }
    for name, value in modules.items(): monkeypatch.setitem(sys.modules, name, value)
    cfg = {'plugin_set': ['*']}
    context = NS(get_config=lambda _: cfg, get_all_stars=lambda: stars)
    event = NS(unified_msg_origin='test', plugins_name=['jiang_commands', 'astrbot_plugin_music', 'jiang_schedule'],
               is_admin=lambda: False, get_platform_name=lambda: 'aiocqhttp',
               get_group_id=lambda: 'test@chatroom', get_self_id=lambda: 'bot')
    pages = module.build_help(context, event)
    result = '\n'.join(pages)
    assert '/新闻' in result and '/查歌词' in result and '/点歌 歌名' in result
    assert all(word not in result for word in ('洛克档案', '管理员测试', '定时新闻', '图来'))
    stars[1].activated = False
    assert '/查歌词' not in '\n'.join(module.build_help(context, event))
    event.is_admin = lambda: True
    assert '/定时新闻 8点' in '\n'.join(module.build_help(context, event))
    assert all(len(page) <= 1800 for page in pages)


@pytest.mark.parametrize('permission,group,is_admin,visible', [
    ('everyone', 'group@chatroom', False, True),
    ('admin', '', False, False),
    ('admin', '', True, True),
    ('group_admin', 'group@chatroom', False, False),
    ('group_admin', 'group@chatroom', True, True),
    ('group_admin', '', False, True),
])
def test_declared_help_uses_shared_permissions(monkeypatch, tmp_path, permission, group, is_admin, visible):
    module = load_help(monkeypatch, tmp_path)
    star = NS(name='sample')
    metadata(tmp_path, star, [{'id': 'schedule', 'description': '只读说明',
                             'permission': permission, 'examples': ['/定时新闻 8点']}])
    event = NS(is_admin=lambda: is_admin, get_group_id=lambda: group)
    assert bool(list(module._declared_help(star, [], event, {}, type('Permission', (), {})))) == visible


def test_help_and_ai_share_declared_examples_and_refresh_handler_state(monkeypatch, tmp_path):
    module = load_help(monkeypatch, tmp_path)
    star = NS(name='sample')
    row = {'id': 'search', 'description': '查询用法来自当前声明', 'handler': 'router',
           'message_pattern': '/查询 .+', 'examples': ['/查询 小猫'], 'effect': 'read'}
    metadata(tmp_path, star, [row])
    handler = NS(handler_module_path='sample', handler_name='router', enabled=True, event_filters=[])
    event = NS(is_admin=lambda: False, get_group_id=lambda: '')
    permission = type('Permission', (), {})
    read = lambda: list(module._declared_help(star, [handler], event, {}, permission))
    assert read() == ['/查询 小猫 — 查询用法来自当前声明']
    handler.enabled = False
    assert read() == []
    handler.enabled = True
    row['examples'] = ['/查询 小狗']
    metadata(tmp_path, star, [row])
    assert read() == ['/查询 小狗 — 查询用法来自当前声明']
    handler.handler_name = 'removed'
    assert read() == []


def test_help_ignores_invalid_declarations_and_retired_schedule(monkeypatch, tmp_path):
    module = load_help(monkeypatch, tmp_path)
    star = NS(name='sample')
    metadata(tmp_path, star, [{'id': 'bad', 'description': '不应展示', 'permission': 'invalid',
                             'examples': ['/错误命令']}])
    event = NS(is_admin=lambda: True, get_group_id=lambda: '')
    assert list(module._declared_help(star, [], event, {}, type('Permission', (), {}))) == []
    source = (ROOT / 'runtime/jiang_commands/command_help.py').read_text(encoding='utf-8')
    declarations = json.loads((ROOT / 'runtime/capability_declarations/jiang_schedule.json').read_text(encoding='utf-8'))
    assert '定时签到' not in source
    assert all('定时签到' not in example for item in declarations['capabilities'] for example in item['examples'])
