"""Restore synthetic backups through delivered UI assets without starting a robot."""
from pathlib import Path

import pytest
from playwright.sync_api import expect

from test_manager_startup import browser, manager_url  # noqa: F401


def _open(page, manager_url):
    source = (Path(__file__).parents[1] / "src/wechatrebot_manager/app_webui/preview.js").read_text("utf-8")
    page.route("**/preview.js*", lambda route: route.fulfill(
        content_type="application/javascript", body=source + """
        const original = window.createPreviewApi;
        window.createPreviewApi = helpers => {
          const controller = original(helpers), factory = controller.previewApi;
          controller.previewApi = () => {
            const api = factory(), bootstrap = api.bootstrap, core = api.get_robot_core, features = api.get_features;
            window.restoreWrites = 0; window.restoreReads = 0;
            api.bootstrap = async () => {
              if (window.restoreWrites) {
                window.restoreReads++;
                if (window.failRestoreRead) return {ok:false,error:{message:'模拟恢复后读取失败',recorded:true}};
              }
              const result = await bootstrap();
              result.data.experience.onboarding_complete = true;
              if (window.restoreWrites) {
                result.data.providers = [{...result.data.providers[0], id:'custom/restored',
                  name:'备份中的模型', model:'restored-model', is_default:true}];
              }
              return result;
            };
            api.restore_backup = async () => {
              window.restoreWrites++;
              return {ok:true,data:{restored_file_count:3,recovery_point_id:'before-restore.zip'}};
            };
            api.get_robot_core = async () => {
              if (window.restoreWrites && window.failCoreRead) return {ok:false,error:{message:'模拟核心配置读取失败',recorded:true}};
              const result = await core();
              result.data.identity.robot_name = window.restoreWrites ? '恢复后的机器人' : '恢复前机器人';
              return result;
            };
            api.get_features = async () => {
              if (window.restoreWrites && window.failFeatureList) return {ok:false,error:{message:'模拟功能列表读取失败',recorded:true}};
              const result = await features();
              result.data.plugins = result.data.plugins.map(item => ({...item,
                display_name: window.restoreWrites ? '恢复后功能' : '恢复前功能'}));
              if (window.holdFeatureRead) await new Promise(resolve => {window.finishFeatureRead = resolve;});
              return result;
            };
            return api;
          };
          return controller;
        };
        """))
    page.goto(manager_url)
    expect(page.locator("#stateTitle")).to_have_text("机器人可用")
    page.locator('.nav-item[data-page="settings"]').click()


def _restore(page):
    page.locator('[data-settings-tab="system"]').click()
    page.locator("#restoreBackup").click()
    expect(page.locator("#modalTitle")).to_have_text("恢复设置与记忆？")
    page.locator("#modalConfirm").click()


@pytest.mark.parametrize("width", [1380, 430])
def test_restore_refreshes_model_snapshot(browser, manager_url, tmp_path, width):
    page = browser.new_page(viewport={"width": width, "height": 860})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        _open(page, manager_url)
        _restore(page)
        expect(page.locator("#modalBackdrop")).to_be_hidden()
        page.locator('[data-settings-tab="model"]').click()
        expect(page.locator("#providerServiceList")).to_contain_text("备份中的模型")
        assert page.evaluate("restoreWrites") == 1
        assert not errors, errors
        assert page.url == manager_url and page.title()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.screenshot(path=str(tmp_path / f"restored-models-{width}.png"))
    finally:
        page.close()


def test_restore_rejects_feature_list_read_started_before_restore(browser, manager_url):
    page = browser.new_page()
    try:
        _open(page, manager_url)
        page.evaluate("window.holdFeatureRead = true")
        page.locator('.nav-item[data-page="features"]').click()
        page.wait_for_function("typeof finishFeatureRead === 'function'")
        page.locator('.nav-item[data-page="settings"]').click()
        _restore(page)
        expect(page.locator("#modalBackdrop")).to_be_hidden()
        page.evaluate("window.holdFeatureRead = false; finishFeatureRead()")
        page.locator('.nav-item[data-page="features"]').click()
        expect(page.locator("#featureList")).to_contain_text("恢复后功能")
        expect(page.locator("#featureList")).not_to_contain_text("恢复前功能")
    finally:
        page.close()


def test_restore_requires_resolving_unsaved_search_draft(browser, manager_url):
    page = browser.new_page()
    try:
        _open(page, manager_url)
        page.locator('[data-settings-tab="search"]').click()
        form = page.locator("#webSearchForm")
        form.locator('label[for="webSearchEnabled"]').click()
        page.locator('[data-settings-tab="system"]').click()
        page.locator("#restoreBackup").click()
        expect(page.locator("#toastRegion")).to_contain_text("尚未保存")
        assert page.evaluate("restoreWrites") == 0
        expect(page.locator("#modalBackdrop")).to_be_hidden()
    finally:
        page.close()


@pytest.mark.parametrize("list_failure", [False, True])
def test_restore_discards_feature_detail_started_before_restore(browser, manager_url, list_failure):
    page = browser.new_page()
    try:
        _open(page, manager_url)
        page.locator('.nav-item[data-page="features"]').click()
        page.locator('.feature-item[data-feature-id="feature-group-analysis"]').click()
        expect(page.locator("#config-basic--analysis_days")).to_be_visible()
        page.evaluate("""value => {
          window.failFeatureList = value;
          const api = window.pywebview.api, read = api.get_feature;
          api.get_feature = async id => {
            const result = await read(id);
            if (id === 'feature-group-analysis') {
              const restored = Boolean(window.restoreWrites);
              result.data.display_name = restored ? '恢复后的设置' : '恢复前的旧设置';
              if (!restored) await new Promise(resolve => {window.finishOldFeature = resolve;});
            }
            return result;
          };
        }""", list_failure)
        page.locator("#refreshFeature").click()
        page.wait_for_function("typeof finishOldFeature === 'function'")
        page.locator('.nav-item[data-page="settings"]').click()
        _restore(page)
        expect(page.locator("#modalBackdrop")).to_be_hidden()
        page.evaluate("window.finishOldFeature()")
        page.locator('.nav-item[data-page="features"]').click()
        if list_failure:
            expect(page.locator("#featureList")).to_contain_text("功能暂时无法读取")
            expect(page.locator("#featureForm")).to_have_count(0)
        else:
            page.locator('.feature-item[data-feature-id="feature-group-analysis"]').click()
            expect(page.locator("#featureDetail")).to_have_attribute("data-feature-name", "恢复后的设置")
    finally:
        page.close()


def test_restore_clears_clean_task_editor_before_returning_to_tasks(browser, manager_url):
    page = browser.new_page()
    try:
        _open(page, manager_url)
        page.locator('.nav-item[data-page="schedules"]').click()
        page.locator("#newScheduledTask").click()
        expect(page.locator("#scheduledTaskEditor")).to_be_visible()
        page.locator('.nav-item[data-page="settings"]').click()
        _restore(page)
        expect(page.locator("#modalBackdrop")).to_be_hidden()
        page.locator('.nav-item[data-page="schedules"]').click()
        expect(page.locator("#scheduledTaskEditor")).to_be_hidden()
        expect(page.locator("#scheduleListView")).to_be_visible()
        page.locator("#newScheduledTask").click()
        expect(page.locator("#scheduledTaskEditor")).to_be_visible()
    finally:
        page.close()


def test_restore_readback_failure_only_retries_reads_and_blocks_stale_pages(browser, manager_url):
    page = browser.new_page()
    try:
        _open(page, manager_url)
        page.evaluate("window.failRestoreRead = true")
        _restore(page)
        expect(page.locator("#modalBackdrop")).to_be_hidden()
        expect(page.locator("#restoreSummary")).to_contain_text("备份已恢复，但界面尚未完成刷新")
        expect(page.locator("#createBackup")).to_be_disabled()
        for selector in ['[data-settings-tab="model"]', '.nav-item[data-page="features"]']:
            page.locator(selector).click()
            expect(page.locator('div[data-settings-section="system"]')).to_be_visible()
        page.locator("#restoreBackup").click()
        expect(page.locator("#restoreBackup")).to_be_enabled()
        assert page.evaluate("restoreWrites") == 1
        assert page.evaluate("restoreReads") == 2
        page.evaluate("""() => {
          pywebview.api.get_maintenance = async () => ({ok:false,error:{message:'模拟维护读取失败',recorded:true}});
        }""")
        page.locator('[data-settings-tab="system"]').click()
        expect(page.locator("#backupSummary")).to_contain_text("维护信息暂时无法读取")
        expect(page.locator("#restoreBackup")).to_be_enabled()
        page.evaluate("window.failRestoreRead = false")
        page.locator("#restoreBackup").click()
        expect(page.locator("#restoreBackup")).to_have_text("选择恢复")
        page.locator('[data-settings-tab="model"]').click()
        expect(page.locator("#providerServiceList")).to_contain_text("备份中的模型")
        assert page.evaluate("restoreWrites") == 1
        assert page.evaluate("restoreReads") == 3
    finally:
        page.close()


@pytest.mark.parametrize("fail_read", [False, True])
def test_restore_invalidates_cached_core_before_reentry(browser, manager_url, fail_read):
    page = browser.new_page()
    try:
        _open(page, manager_url)
        page.locator('[data-settings-tab="core"]').click()
        expect(page.locator('#coreForm [name="robot_name"]')).to_have_value("恢复前机器人")
        page.evaluate("value => {window.failCoreRead = value;}", fail_read)
        _restore(page)
        expect(page.locator("#modalBackdrop")).to_be_hidden()
        page.locator('[data-settings-tab="core"]').click()
        if fail_read:
            expect(page.locator("#coreDetail")).to_contain_text("核心配置暂时无法读取")
            expect(page.locator('#coreForm [name="robot_name"]')).to_have_count(0)
        else:
            expect(page.locator('#coreForm [name="robot_name"]')).to_have_value("恢复后的机器人")
    finally:
        page.close()
