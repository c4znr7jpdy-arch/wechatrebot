import ast
import asyncio
import importlib.util
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from wechatrebot_manager.core_reply_identity import apply_reply_identity


ROOT = Path(__file__).resolve().parents[1]
SOURCE = Path('E:/Project/data/plugins/astrbot_plugin_angel_heart')
PATCH = ROOT / 'patches/astrbot-plugin-astrbot_plugin_angel_heart.patch'


def test_core_identity_preserves_system_rules_and_replaces_its_own_section():
    request = SimpleNamespace(system_prompt='Safety rules. Tools and memory stay here.', contexts=[{'role': 'user', 'content': 'hello'}])
    config = SimpleNamespace(ai_self_identity='You are the {group} assistant.', reply_strategy_guide='Be concise.', alias='小群|群助手')
    apply_reply_identity(request, config)
    assert request.system_prompt.startswith('Safety rules. Tools and memory stay here.')
    assert 'You are the {group} assistant.' in request.system_prompt
    assert '聊天称呼：小群' in request.system_prompt
    assert request.contexts == [{'role': 'user', 'content': 'hello'}]
    config.ai_self_identity = 'Changed live identity'
    apply_reply_identity(request, config)
    assert request.system_prompt.count('[robot_core_profile]') == 1
    assert 'Changed live identity' in request.system_prompt
    assert 'You are the {group} assistant.' not in request.system_prompt
    config.ai_self_identity = config.reply_strategy_guide = config.alias = ''
    apply_reply_identity(request, config)
    assert request.system_prompt == 'Safety rules. Tools and memory stay here.'


def _module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def patched_core(tmp_path):
    if not (SOURCE / 'main.py').is_file():
        pytest.skip('Conversation core source checkout is unavailable')
    repository = tmp_path / 'patched-core'
    (repository / 'web_api').mkdir(parents=True)
    for relative in ('main.py', 'web_api/__init__.py'):
        shutil.copy2(SOURCE / relative, repository / relative)
    subprocess.run(['git', 'init', '--quiet'], cwd=repository, check=True, capture_output=True)
    checked = subprocess.run(['git', 'apply', '--check', '--whitespace=error-all', str(PATCH)], cwd=repository, capture_output=True, text=True)
    assert checked.returncode == 0, checked.stderr
    subprocess.run(['git', 'apply', '--whitespace=error-all', str(PATCH)], cwd=repository, check=True, capture_output=True)
    helper = (repository / 'core/reply_identity.py').read_text(encoding='utf-8')
    assert helper == (ROOT / 'src/wechatrebot_manager/core_reply_identity.py').read_text(encoding='utf-8')
    for relative in ('main.py', 'web_api/__init__.py', 'core/reply_identity.py'):
        ast.parse((repository / relative).read_text(encoding='utf-8-sig'))
    return repository


def test_reply_hook_uses_live_templates_on_first_message_and_respects_whitelist(patched_core, tmp_path):
    # Exercise the actual patched hook and original config/template store,
    # without importing or touching a running engine, plugins or accounts.
    tree = ast.parse((patched_core / 'main.py').read_text(encoding='utf-8-sig'))
    plugin = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'AngelHeartPlugin')
    method = next(node for node in plugin.body if isinstance(node, ast.AsyncFunctionDef) and node.name == 'delegate_prompt_rewriting')
    method.decorator_list = []
    namespace = {'apply_reply_identity': apply_reply_identity, 'AstrMessageEvent': object, 'ProviderRequest': object, 'logger': SimpleNamespace(debug=lambda *args: None)}
    exec(compile(ast.Module(body=[method], type_ignores=[]), '<patched-core-hook>', 'exec'), namespace)
    ConfigManager = _module(SOURCE / 'core/config_manager.py', '_test_core_config').ConfigManager
    Store = _module(SOURCE / 'core/chat_profile.py', '_test_core_profiles').ChatProfileStore
    store = Store(str(tmp_path))
    global_config = {
        'personality': {'ai_self_identity': 'global identity', 'reply_strategy_guide': 'global guide'},
        'wake_interaction': {'alias': '全局|助手'},
        'access_control': {'whitelist_enabled': True, 'group_chat_enhancement': False, 'takeover_private_chat_context': False},
    }
    manager = ConfigManager(global_config)
    manager.attach_profile_store(store)
    template = store.create_template('group', config={'personality': {'ai_self_identity': 'group identity'}, 'wake_interaction': {'alias': '本群|群助手'}})
    group_id = 'wechat:GroupMessage:room@chatroom'
    store.set_binding(group_id, template['id'])
    owner = SimpleNamespace(config_manager=manager, _whitelist_cache={'room@chatroom', 'friend'}, _get_plain_chat_id=lambda chat: chat.split(':')[-1], _is_private_chat=lambda chat: ':FriendMessage:' in chat)
    hook = namespace['delegate_prompt_rewriting']
    def invoke(chat):
        request = SimpleNamespace(system_prompt='Safety and tools')
        asyncio.run(hook(owner, SimpleNamespace(unified_msg_origin=chat), request))
        return request.system_prompt
    assert 'group identity' in invoke(group_id)
    assert '聊天称呼：本群' in invoke(group_id)
    assert 'global guide' in invoke(group_id)
    assert 'global identity' in invoke('wechat:FriendMessage:friend')
    assert invoke('wechat:GroupMessage:other@chatroom') == 'Safety and tools'
    store.update_template(template['id'], {'config': {'personality': {'ai_self_identity': 'live changed group identity'}}})
    assert 'live changed group identity' in invoke(group_id)
    assert '聊天称呼：全局' in invoke(group_id)
    store.set_binding(group_id, '')
    assert 'global identity' in invoke(group_id)
