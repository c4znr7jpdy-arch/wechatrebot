import json
from pathlib import Path

import pytest

from wechatrebot_manager import app_shell
from wechatrebot_manager.brand import BrandProfile, save_brand_profile
from wechatrebot_manager.customer_config import RobotSettings, load_robot_settings, save_robot_settings
from wechatrebot_manager.customer_logs import sanitize_log_text
from wechatrebot_manager.diagnostics import white_label_terms
from wechatrebot_manager.group_policy import GroupPolicyService
from wechatrebot_manager.private_policy import PrivateChatPolicyService
from wechatrebot_manager.core_settings import RobotCoreSettingsService
from wechatrebot_manager.plugin_center import (
    ConfigConflictError, PluginCenterError, PluginCenterService,
    ROBOT_CORE_FEATURE_ID, _white_label_core_page_content,
)
from wechatrebot_manager.runtime_config import write_runtime_configs


def _service(tmp_path: Path) -> RobotCoreSettingsService:
    directory = tmp_path / 'app/astrbot/data/plugins/astrbot_plugin_angel_heart'
    (directory / 'core').mkdir(parents=True)
    (directory / 'pages/chat-config').mkdir(parents=True)
    (directory / 'pages/chat-config/index.html').write_text('<h1>天使之心</h1>', encoding='utf-8')
    (directory / 'core/chat_profile.py').write_text('TEMPLATE_GROUPS: dict = {"personality": {"ai_self_identity": None}, "energy": {"initial_energy": None}}', encoding='utf-8')
    schema = {
        'analyzer_model': {'type': 'string', 'default': '', '_special': 'select_provider'},
        'personality': {'type': 'object', 'description': 'AngelHeart 助理画像', 'items': {
            'ai_self_identity': {'type': 'text', 'description': '助理身份', 'default': '你拥有强大的检索能力和庞大的知识储备和记忆能力。\n你喜爱跟群友进行语言游戏沟通。\n群友对AI的善意测试你必须回应满足群友的期待，展示你的能力。'},
            'reply_strategy_guide': {'type': 'text', 'default': 'guide'},
        }},
        'wake_interaction': {'type': 'object', 'items': {
            'alias': {'type': 'string', 'default': 'AI|助手'},
        }},
        'energy': {'type': 'object', 'items': {'initial_energy': {'type': 'float', 'default': 100}}},
        'context_compression': {'type': 'object', 'items': {'enabled': {'type': 'bool', 'default': True}}},
    }
    (directory / '_conf_schema.json').write_text(json.dumps(schema), encoding='utf-8')
    features = PluginCenterService(tmp_path)
    features.engine.is_available = lambda: False
    return RobotCoreSettingsService(features)


def test_core_is_single_settings_entry_and_exposes_all_schema_fields(tmp_path):
    service = _service(tmp_path)
    detail = service.detail()
    assert service.features.catalog()['plugins'] == []
    assert detail['id'] == ROBOT_CORE_FEATURE_ID
    assert detail['display_name'] == '机器人核心'
    assert set(detail['advanced_schema']) == {'analyzer_model', 'personality', 'wake_interaction', 'energy', 'context_compression'}
    assert detail['advanced_config']['context_compression']['enabled'] is True
    assert set(detail['template_schema']) == {'personality', 'energy'}
    assert set(detail['template_schema']['personality']['items']) == {'ai_self_identity'}
    assert 'AngelHeart' not in json.dumps(detail['advanced_schema'], ensure_ascii=False)
    assert '助理' not in json.dumps(detail['advanced_schema'], ensure_ascii=False)
    assert '助手' not in json.dumps(detail['advanced_schema'], ensure_ascii=False)
    assert detail['identity'] == {'robot_name': '小妹', 'admin_id': ''}
    assert detail['advanced_config']['wake_interaction']['alias'] == '小妹'
    assert detail['advanced_config']['personality']['ai_self_identity'].startswith('你是小妹')
    config = detail['advanced_config']
    config['personality']['ai_self_identity'] = 'actual identity'
    config['energy']['initial_energy'] = 80
    result = service.features.save(detail['id'], config, detail['revision'])
    assert result['apply_state'] == 'pending_start'
    assert service.detail()['advanced_config']['energy']['initial_energy'] == 80
    with pytest.raises(ConfigConflictError):
        service.features.save(detail['id'], config, detail['revision'])


def test_core_template_api_is_scoped_validated_and_checks_stale_updates(tmp_path):
    service = _service(tmp_path)
    calls = []
    service.features.extension_path = lambda _id, endpoint: '/core/' + endpoint
    def request(method, path, body=None):
        calls.append((method, path, body))
        if path == '/core/profiles':
            return {'templates': [{'id': 'tpl_one', 'updated_at': 5}]}
        return {'saved': True}
    service.features.engine.request = request
    for method, endpoint in [('DELETE', 'profiles'), ('GET', '../config'), ('POST', 'plugin_config/save')]:
        with pytest.raises(PluginCenterError, match='请求无效'):
            service.request(method, endpoint, {})
    with pytest.raises(PluginCenterError, match='不支持的配置项'):
        service.request('POST', 'profiles/update', {'config': {'personality': {'reply_strategy_guide': 'not supported by this store'}}})
    with pytest.raises(PluginCenterError, match='值类型'):
        service.request('POST', 'profiles/update', {'config': {'energy': {'initial_energy': 'bad'}}})
    with pytest.raises(ConfigConflictError):
        service.request('POST', 'profiles/update', {'id': 'tpl_one', 'updated_at': 4})
    payload = {'id': 'tpl_one', 'updated_at': 5, 'config': {'energy': {'initial_energy': 80}}}
    assert service.request('POST', 'profiles/update', payload) == {'saved': True}
    assert payload['updated_at'] == 5
    assert calls[-1] == ('POST', '/core/profiles/update', {'id': 'tpl_one', 'config': {'energy': {'initial_energy': 80}}})
    assert service.request('POST', 'bindings/set', {'chat_id': 'group@chatroom', 'template_id': ''})['saved']


def test_core_identity_save_updates_name_admin_alias_and_reply_persona(tmp_path):
    service = _service(tmp_path)
    detail = service.detail()
    config = detail['advanced_config']
    config['wake_interaction']['alias'] = 'AI|助手|妹宝'
    config['personality']['ai_self_identity'] = '你是\u59dc\u5c0f\u59b9，是可靠的群聊助手。'

    saved = service.save_global(
        config,
        detail['revision'],
        {'robot_name': '阿妹', 'admin_id': 'wxid_admin'},
    )

    assert saved['brand']['display_name'] == '阿妹'
    assert saved['brand']['admin_id'] == 'wxid_admin'
    stored = json.loads((tmp_path / 'app/astrbot/data/config/astrbot_plugin_angel_heart_config.json').read_text(encoding='utf-8'))
    assert stored['wake_interaction']['alias'] == '阿妹|妹宝'
    assert stored['personality']['ai_self_identity'] == '你是阿妹，是可靠的群聊机器人。'
    engine = json.loads((tmp_path / 'app/astrbot/data/cmd_config.json').read_text(encoding='utf-8'))
    assert engine['admins_id'] == ['wxid_admin']
    persona = next(item for item in engine['persona'] if item['name'] == 'commercial_brand')
    assert '你是阿妹' in persona['prompt']


def test_core_identity_rejects_invalid_admin_before_changing_core(tmp_path):
    service = _service(tmp_path)
    detail = service.detail()
    with pytest.raises(ValueError, match='管理员 wxid 格式无效'):
        service.save_global(
            detail['advanced_config'],
            detail['revision'],
            {'robot_name': '小妹', 'admin_id': 'not valid'},
        )
    assert not (tmp_path / 'app/astrbot/data/config/astrbot_plugin_angel_heart_config.json').exists()


def test_core_identity_preserves_a_distinct_assistant_in_custom_persona(tmp_path):
    brand_path = tmp_path / 'config/brand.json'
    brand_path.parent.mkdir(parents=True)
    save_brand_profile(
        brand_path,
        BrandProfile('小妹', '小妹', '小妹', welcome_text='小妹来了，大家好。'),
    )
    service = _service(tmp_path)
    detail = service.detail()
    config = detail['advanced_config']
    config['personality']['ai_self_identity'] = '你是小妹；群友的私人助理是另一个人。'
    service.save_global(
        config,
        detail['revision'],
        {'robot_name': '阿妹', 'admin_id': ''},
    )
    stored = json.loads((tmp_path / 'app/astrbot/data/config/astrbot_plugin_angel_heart_config.json').read_text(encoding='utf-8'))
    assert stored['personality']['ai_self_identity'] == '你是阿妹；群友的私人助理是另一个人。'
    assert json.loads(brand_path.read_text(encoding='utf-8'))['welcome_text'] == '阿妹来了，大家好。'


def test_old_factory_brand_preview_keeps_custom_welcome_text(tmp_path):
    brand_path = tmp_path / 'config/brand.json'
    brand_path.parent.mkdir(parents=True)
    old_name = '\u59dc\u5c0f\u59b9\u673a\u5668\u4eba'
    brand_path.write_text(json.dumps({
        'display_name': old_name,
        'short_name': '\u59dc\u5c0f\u59b9',
        'persona_name': '\u59dc\u5c0f\u59b9',
        'welcome_text': f'大家好，我是{old_name}，欢迎使用。',
    }, ensure_ascii=False), encoding='utf-8')
    profile = load_robot_settings(tmp_path).profile
    assert profile.display_name == '小妹'
    assert profile.welcome_text == '大家好，我是小妹，欢迎使用。'


def test_core_page_rebranding_preserves_module_and_api_paths():
    raw = '<h1>天使之心</h1>AngelHeart 天使心 kawayiYokami 插件设置 助理昵称 const module="astrbot_plugin_angel_heart";'.encode()
    path = '/api/plugin/page/content/astrbot_plugin_angel_heart/chat-config/assets/main.js'
    result = _white_label_core_page_content(path, 'application/javascript; charset=utf-8', raw).decode()
    assert '天使之心' not in result and 'AngelHeart' not in result
    assert '天使心' not in result and 'kawayiYokami' not in result
    assert '助理昵称' not in result and '机器人昵称' in result
    assert '机器人核心' in result and 'astrbot_plugin_angel_heart' in result
    assert _white_label_core_page_content('/other/content/main.js', 'text/javascript', raw) == raw
    assert _white_label_core_page_content(path, 'image/png', raw) == raw


def test_customer_logs_and_diagnostics_do_not_expose_core_brand_or_author():
    text = 'AngelHeart / angel_heart / 天使之心 / 天使心 / kawayiYokami api_key=secret'
    log = sanitize_log_text(text)
    report = json.dumps(white_label_terms({'message': text}), ensure_ascii=False)
    for original in ('AngelHeart', 'angel_heart', '天使之心', '天使心', 'kawayiYokami'):
        assert original not in log + report
    assert 'secret' not in log


def test_core_ui_replaces_persona_and_keeps_templates_and_monitor():
    webui = Path(app_shell.__file__).with_name('app_webui')
    html = (webui / 'index.html').read_text(encoding='utf-8')
    app = (webui / 'app.js').read_text(encoding='utf-8')
    core = (webui / 'core-settings.js').read_text(encoding='utf-8')
    assert 'data-settings-tab="core"' in html
    assert '人格身份与维护' not in html
    assert 'personaForm' not in html + app
    assert 'brandForm' not in html + app
    assert 'save_persona_settings' not in app_shell.PUBLIC_API_METHODS
    assert {'get_robot_core', 'request_robot_core'} <= set(app_shell.PUBLIC_API_METHODS)
    assert 'data-settings-section="system"' in html
    assert 'id="createBackup"' in html and 'id="openUninstaller"' in html
    assert '专业设置（全部）' in core
    assert '机器人名称与管理员' in core
    assert 'name="robot_name"' in core and 'name="admin_id"' in core
    assert 'api("save_robot_core"' in app
    for endpoint in ['profiles/create', 'profiles/update', 'profiles/delete', 'bindings/set', 'chat_status']:
        assert endpoint in core
    assert '覆盖全局' in core and '恢复此组默认值' in core
    assert 'coreTemplateSource' in html and 'template_description' in core
    assert 'bindingKey(select.dataset.coreBinding)' in core
    assert 'coreSettings.guard' in app and 'coreSettings.busy' in app
    assert '聊天状态与配置' in html + core
    assert '联系人监控与绑定' not in html + core
    assert 'state.groups.find((entry) => entry.group_id === canonical)' in core
    assert 'state.contacts.find((entry) => entry.user_id === canonical)' in core
    assert '等待微信目录同步' in core
    assert 'getGroups: (refresh = false) => api("get_groups", refresh)' in app
    assert '天使之心' not in html + core and 'AngelHeart' not in html + core


def test_runtime_sync_does_not_overwrite_core_wake_settings(tmp_path):
    write_runtime_configs(tmp_path)
    directory = tmp_path / 'app/astrbot/data/plugins/astrbot_plugin_angel_heart'
    directory.mkdir(parents=True)
    config_path = tmp_path / 'app/astrbot/data/config/astrbot_plugin_angel_heart_config.json'
    expected = {'wake_interaction': {'enter_on_mention_only': True, 'direct_wake_fast_path': False}, 'personality': {'ai_self_identity': 'keep me'}}
    config_path.write_text(json.dumps(expected), encoding='utf-8')
    write_runtime_configs(tmp_path)
    assert json.loads(config_path.read_text(encoding='utf-8')) == expected


def test_interface_name_save_preserves_chat_persona_and_core(tmp_path):
    write_runtime_configs(tmp_path)
    engine_path = tmp_path / 'app/astrbot/data/cmd_config.json'
    engine = json.loads(engine_path.read_text(encoding='utf-8'))
    persona = [{'name': 'existing', 'prompt': 'saved reply identity'}]
    engine['persona'] = persona
    engine['provider_settings']['default_personality'] = 'existing'
    engine_path.write_text(json.dumps(engine), encoding='utf-8')
    directory = tmp_path / 'app/astrbot/data/plugins/astrbot_plugin_angel_heart'
    directory.mkdir(parents=True)
    config_path = tmp_path / 'app/astrbot/data/config/astrbot_plugin_angel_heart_config.json'
    core = {'personality': {'ai_self_identity': 'saved core identity'}, 'wake_interaction': {'enter_on_mention_only': True, 'direct_wake_fast_path': False}}
    config_path.write_text(json.dumps(core), encoding='utf-8')
    result = save_robot_settings(tmp_path, RobotSettings(BrandProfile('管理新名称', '管理', '旧称呼'), 'wxid_admin'), apply_reply_identity=False)
    updated = json.loads(engine_path.read_text(encoding='utf-8'))
    assert result.profile.display_name == '管理新名称'
    assert updated['persona'] == persona
    assert updated['provider_settings']['default_personality'] == 'existing'
    assert json.loads(config_path.read_text(encoding='utf-8')) == core


def test_template_schema_supports_plain_whitelist_and_fails_safe(tmp_path):
    service = _service(tmp_path)
    path = tmp_path / 'app/astrbot/data/plugins/astrbot_plugin_angel_heart/core/chat_profile.py'
    path.write_text('TEMPLATE_GROUPS = {"energy": {"initial_energy": None}}', encoding='utf-8')
    assert set(service.detail()['template_schema']) == {'energy'}
    path.write_text('TEMPLATE_GROUPS = dynamic_configuration()', encoding='utf-8')
    assert service.detail()['template_schema'] == {}


def test_core_is_not_optional_in_group_or_private_feature_scopes(tmp_path):
    path = tmp_path / 'legal/feature-set.json'
    path.parent.mkdir()
    core_module = 'astrbot_plugin_angel_heart'
    path.write_text(json.dumps({'features': [
        {'module': core_module, 'display_name': '群聊上下文', 'configuration': 'schema-and-page', 'default_enabled': True},
        {'module': 'astrbot_plugin_music', 'display_name': '点歌', 'configuration': 'none', 'default_enabled': True},
    ]}), encoding='utf-8')
    group = GroupPolicyService(tmp_path)
    private = PrivateChatPolicyService(tmp_path)
    assert [item['name'] for item in group.overview('wxid_bot', 'room@chatroom')['features']] == ['点歌']
    assert [item['name'] for item in private.overview('wxid_bot', 'wxid_friend')['features']] == ['点歌']
    assert group.save('wxid_bot', 'room@chatroom', {'allowed_features': [], 'bot_enabled': False})['allowed_features'] == [core_module]
    assert private.save('wxid_bot', 'wxid_friend', {'allowed_features': [], 'bot_enabled': False})['allowed_features'] == [core_module]
    assert group.get('wxid_bot', 'room@chatroom')['bot_enabled'] is False
    assert private.overview('wxid_bot', 'wxid_friend')['policy']['bot_enabled'] is False
