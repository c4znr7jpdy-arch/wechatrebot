import ast
import asyncio
import importlib.util
from pathlib import Path
import re
import shutil
import subprocess
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
SOURCE = Path('E:/Project/data/plugins/jiang_commands/main.py')
OIL_SOURCE = Path('E:/Project/ai_plugin/ai_plugin/oilprice.py')


@pytest.mark.parametrize('message, province', [
    ('山东油价', '山东'), ('油价山东', '山东'), ('山东 油价', '山东'),
    ('油价 山东', '山东'), ('油价', ''), ('新闻', None), ('山东天气', None),
])
def test_declared_oilprice_pattern_without_source_checkout(message, province):
    patch = ROOT / 'patches/astrbot-plugin-jiang_commands--oilprice.patch'
    line = next(line[1:] for line in patch.read_text(encoding='utf-8').splitlines()
                if line.startswith('+_OILPRICE_PATTERN ='))
    declaration = ast.parse(line).body[0]
    pattern = re.compile(ast.literal_eval(declaration.value.args[0]))
    match = pattern.fullmatch(message)
    actual = (match[1] or match[2] or '').strip() if match else None
    assert actual == province


@pytest.fixture(scope='module')
def patched_tree(tmp_path_factory):
    if not SOURCE.is_file() or not OIL_SOURCE.is_file():
        pytest.skip('Commands source checkout is unavailable')
    commercial = tmp_path_factory.mktemp('oilprice-command')
    output = commercial / 'artifacts/staging'
    plugin = output / 'app/astrbot/data/plugins/jiang_commands'
    plugin.mkdir(parents=True)
    # The staging patch set covers the whole plugin, not just main.py.  Copy
    # the source tree so schema and asset patches are exercised together with
    # the oil-price handler.
    shutil.copytree(
        SOURCE.parent,
        plugin,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '.git'),
    )
    patches = commercial / 'patches'
    patches.mkdir()
    for patch in ROOT.glob('patches/astrbot-plugin-jiang_commands*.patch'):
        shutil.copy2(patch, patches / patch.name)
    subprocess.run(['git', 'init', '--quiet'], cwd=commercial, check=True, capture_output=True)
    spec = importlib.util.spec_from_file_location('_oilprice_staging', ROOT / 'scripts/build_staging.py')
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    # Exercise only the lightweight patch step, not a build or release.
    builder._apply_plugin_patches(commercial, output)
    for patch in patches.glob('*.patch'):
        assert (output / 'legal/corresponding-source' / patch.name).read_bytes() == patch.read_bytes()
    return ast.parse((plugin / 'main.py').read_text(encoding='utf-8-sig'))


def _handler(tree, *, fail=False):
    nodes = [node for node in tree.body if (
        isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == '_OILPRICE_PATTERN' for t in node.targets)
    ) or (
        isinstance(node, ast.FunctionDef) and node.name == '_strip_system_identity_prefix'
    )]
    owner = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'Main')
    method = next(node for node in owner.body if isinstance(node, ast.AsyncFunctionDef) and node.name == 'oilprice')
    # Run the real handler in isolation: no plugin registration, running bot or sends.
    method = ast.parse(ast.unparse(method)).body[0]
    method.decorator_list = []
    nodes.append(method)
    formatter = next(node for node in ast.parse(OIL_SOURCE.read_text(encoding='utf-8')).body
                     if isinstance(node, ast.FunctionDef) and node.name == 'format_oilprice')
    nodes.append(formatter)
    calls = []
    actions = []

    async def fetch(province):
        calls.append(province)
        if fail:
            raise RuntimeError('测试接口暂不可用')
        return {'province': {'pri_name': province, 'gasoline_92': '1.00', 'gasoline_95': '2.00',
                             'gasoline_98': '3.00', 'diesel_0': '4.00'}, 'time': 'test fixture'}

    namespace = {'re': re, 'AstrMessageEvent': object, 'fetch_oilprice': fetch,
                 '_record_event_action': lambda *args: actions.append(args),
                 'logger': SimpleNamespace(exception=lambda *args: None)}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), '<patched-oilprice>', 'exec'), namespace)

    def invoke(message):
        event = SimpleNamespace(get_message_str=lambda: message, plain_result=lambda text: text)
        async def collect():
            return [text async for text in namespace['oilprice'](None, event)]
        return asyncio.run(collect()), calls, actions

    return invoke


@pytest.mark.parametrize('message, province', [
    ('/山东油价', '山东'), ('/油价山东', '山东'),
    ('\\山东油价', '山东'), ('\\油价 山东', '山东'),
    (' / 山东 油价 ', '山东'), (' / 油价 山东 ', '山东'),
    ('四川油价', '四川'), ('油价四川', '四川'),
    ('/北京油价', '北京'), ('/内蒙古油价', '内蒙古'),
    ('[系统身份提示：测试身份]\n/山东油价', '山东'),
])
def test_oilprice_supports_both_province_orders(patched_tree, message, province):
    results, calls, actions = _handler(patched_tree)(message)
    assert calls == [province]
    assert len(results) == 1 and f'{province}今日油价' in results[0]
    assert '92# 汽油' in results[0] and '1.00/升' in results[0]
    assert len(actions) == 1 and actions[0][1] == 'info.oilprice'
    assert actions[0][3] == {'province': province}


@pytest.mark.parametrize('message', ['/油价', '\\油价', '油价', ' / 油价   '])
def test_oilprice_without_province_has_a_usage_reply(patched_tree, message):
    results, calls, actions = _handler(patched_tree)(message)
    assert len(results) == 1 and '请指定省份' in results[0]
    assert '/山东油价' in results[0] and '/油价山东' in results[0]
    assert calls == actions == []


@pytest.mark.parametrize('message', ['/山东天气', '/新闻', '/山东油价明天', '讨论油价上涨了吗'])
def test_oilprice_does_not_handle_unrelated_messages(patched_tree, message):
    results, calls, actions = _handler(patched_tree)(message)
    assert results == calls == actions == []


def test_oilprice_fetch_failure_replies_instead_of_silence(patched_tree):
    results, calls, actions = _handler(patched_tree, fail=True)('/山东油价')
    assert calls == ['山东'] and actions == []
    assert len(results) == 1 and '获取油价失败' in results[0]


def test_oilprice_help_shows_both_orders(patched_tree):
    source = (ROOT / 'runtime/jiang_commands/command_help.py').read_text(encoding='utf-8')
    assert '/山东油价 或 /油价山东' in source
