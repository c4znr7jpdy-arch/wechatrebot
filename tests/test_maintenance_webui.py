"""Maintenance entry flows use preview fixtures and never start a robot."""
import pytest
from playwright.sync_api import expect

from test_manager_startup import browser, manager_url  # noqa: F401


def _open_preview(browser, manager_url, width=1380):
    page = browser.new_page(viewport={"width": width, "height": 860})
    page.goto(manager_url)
    expect(page.locator("#stateTitle")).to_have_text("机器人可用")
    if page.locator("#onboardingRecommended").is_visible():
        page.locator("#onboardingRecommended").click()
    return page


def test_system_reentry_refreshes_backup_gate_after_stop(browser, manager_url, tmp_path):
    page = _open_preview(browser, manager_url)
    try:
        page.evaluate("""() => {
          const api = window.pywebview.api, read = api.get_maintenance, status = api.get_status;
          window.maintenanceReads = 0;
          api.get_maintenance = async () => {
            window.maintenanceReads++;
            const result = await read();
            result.data.preview.can_create = !(await status()).data.runtime.engine_ready;
            if (window.delayMaintenance) return new Promise(resolve => {
              window.finishMaintenance = () => resolve(result);
            });
            return result;
          };
        }""")
        page.locator('.nav-item[data-page="settings"]').click()
        page.locator('[data-settings-tab="system"]').click()
        expect(page.locator("#backupSummary")).to_contain_text("请先停止机器人")
        expect(page.locator("#createBackup")).to_be_disabled()
        expect(page.locator("#restoreBackup")).to_be_disabled()
        page.locator("#backupSummary").scroll_into_view_if_needed()
        page.screenshot(path=str(tmp_path / "maintenance-running.png"))

        page.locator('.nav-item[data-page="home"]').click()
        page.locator("#primaryAction").click()
        expect(page.locator("#stateTitle")).to_have_text("机器人尚未启动")
        page.evaluate("window.delayMaintenance = true")
        page.locator('.nav-item[data-page="settings"]').click()
        expect(page.locator("#backupSummary")).to_have_text("正在重新检查备份条件…")
        expect(page.locator("#createBackup")).to_be_disabled()
        expect(page.locator("#restoreBackup")).to_be_disabled()
        page.wait_for_function("typeof window.finishMaintenance === 'function'")
        page.evaluate("window.finishMaintenance(); window.delayMaintenance = false")
        expect(page.locator("#backupSummary")).to_contain_text("现在可以备份")
        expect(page.locator("#createBackup")).to_be_enabled()
        expect(page.locator("#restoreBackup")).to_be_enabled()
        page.locator("#createBackup").click()
        expect(page.locator("#modalTitle")).to_have_text("创建设置与记忆备份？")
        page.locator("#modalCancel").click()
        page.locator("#backupSummary").scroll_into_view_if_needed()
        page.screenshot(path=str(tmp_path / "maintenance-stopped.png"))
        assert page.evaluate("window.maintenanceReads") == 2
        assert page.url == manager_url and page.title()
    finally:
        page.close()


def test_maintenance_read_failure_and_old_response_cannot_enable_backup(browser, manager_url):
    page = _open_preview(browser, manager_url)
    try:
        page.evaluate("""() => {
          const api = window.pywebview.api, read = api.get_maintenance;
          window.maintenanceRequests = [];
          api.get_maintenance = async () => {
            const result = await read();
            return new Promise(resolve => window.maintenanceRequests.push({resolve, result}));
          };
        }""")
        page.locator('.nav-item[data-page="settings"]').click()
        system = page.locator('[data-settings-tab="system"]')
        system.click()
        page.wait_for_function("window.maintenanceRequests.length === 1")
        system.click()
        page.wait_for_function("window.maintenanceRequests.length === 2")
        page.evaluate("""() => {
          maintenanceRequests[1].resolve({ok:false,error:{code:'19000',
            message:'模拟维护读取失败',recorded:true}});
        }""")
        expect(page.locator("#backupSummary")).to_contain_text("重新打开“系统”重试")
        page.evaluate("maintenanceRequests[0].resolve(maintenanceRequests[0].result)")
        expect(page.locator("#createBackup")).to_be_disabled()
        expect(page.locator("#restoreBackup")).to_be_disabled()
        expect(page.locator("#backupSummary")).to_contain_text("重新打开“系统”重试")
        system.click()
        page.wait_for_function("window.maintenanceRequests.length === 3")
        page.evaluate("maintenanceRequests[2].resolve(maintenanceRequests[2].result)")
        expect(page.locator("#createBackup")).to_be_enabled()
        expect(page.locator("#restoreBackup")).to_be_enabled()
    finally:
        page.close()


@pytest.mark.parametrize("width", [1380, 430])
def test_diagnostic_export_location_persists_and_copies(browser, manager_url, tmp_path, width):
    page = _open_preview(browser, manager_url, width)
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
    try:
        export_path = "C:\\Users\\示例用户\\Documents\\我的机器人安装目录\\logs\\robot-diagnostics-20261108-123456.json"
        page.evaluate("""path => {
          const api = window.pywebview.api;
          window.diagnosticExports = 0;
          Object.defineProperty(navigator, 'clipboard', {configurable:true,
            value:{writeText:async text => {window.copiedDiagnosticPath = text;}}});
          api.export_diagnostics = () => {
            window.diagnosticExports++;
            return new Promise(resolve => {window.finishDiagnosticExport = success => resolve(success
              ? {ok:true,data:{path,file_name:'robot-diagnostics-20261108-123456.json'}}
              : {ok:false,error:{code:'19000',message:'模拟导出失败',recorded:true}});});
          };
        }""", export_path)
        page.locator('.nav-item[data-page="settings"]').click()
        page.locator('[data-settings-tab="system"]').click()
        expect(page.locator("#diagnosticExportLocation")).to_be_hidden()
        export = page.locator("#exportDiagnostics")
        export.click()
        expect(export).to_be_disabled()
        export.evaluate("button => button.click()")
        assert page.evaluate("window.diagnosticExports") == 1
        page.evaluate("window.finishDiagnosticExport(true)")
        expect(export).to_be_enabled()
        expect(page.locator("#diagnosticExportFeedback")).to_contain_text("已保存")
        expect(page.locator("#diagnosticExportPath")).to_have_text(export_path)
        copy = page.locator("#copyDiagnosticPath")
        copy.click()
        assert page.evaluate("window.copiedDiagnosticPath") == export_path
        expect(page.locator("#toastRegion")).to_contain_text("保存位置已复制")
        page.locator('.nav-item[data-page="home"]').click()
        page.locator('.nav-item[data-page="settings"]').click()
        expect(page.locator("#diagnosticExportPath")).to_have_text(export_path)
        copy.scroll_into_view_if_needed()
        page.screenshot(path=str(tmp_path / f"diagnostic-location-{width}.png"))
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        export.click()
        page.evaluate("window.finishDiagnosticExport(false)")
        expect(page.locator("#diagnosticExportFeedback")).to_contain_text("未能导出")
        expect(export).to_be_enabled()
        expect(copy).to_be_enabled()
        expect(page.locator("#diagnosticExportPath")).to_have_text(export_path)
        assert not errors, errors
        assert page.url == manager_url and page.title()
    finally:
        page.close()
