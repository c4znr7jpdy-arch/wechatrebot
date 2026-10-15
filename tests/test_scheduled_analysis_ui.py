"""Verify the unified analysis editor through the delivered manager pages."""
import pytest
from playwright.sync_api import expect

from test_manager_startup import browser, manager_url


def open_schedules(page, url):
    page.goto(url)
    expect(page.locator("#versionText")).not_to_have_text("v0.0.0")
    if page.locator("#onboardingRecommended").is_visible():
        page.locator("#onboardingRecommended").click()
    page.evaluate("""() => {
      window.taskCalls = [];
      for (const name of ['save_scheduled_task', 'set_scheduled_task_enabled',
          'delete_scheduled_task', 'preview_scheduled_task', 'test_scheduled_task', 'save_feature']) {
        const original = window.pywebview.api[name];
        window.pywebview.api[name] = (...args) => {
          window.taskCalls.push({name, args: structuredClone(args)});
          return original(...args);
        };
      }
    }""")
    page.locator('.nav-item[data-page="schedules"]').click()
    expect(page.locator("#refreshSchedules")).to_be_enabled()


def test_analysis_uses_persisted_tasks_for_draft_preview_toggle_and_delete(browser, manager_url, tmp_path):
    page = browser.new_page(viewport={"width": 1380, "height": 860})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        open_schedules(page, manager_url)
        page.locator("#newScheduledTask").click()
        page.locator("#scheduledAction").select_option("group_analysis")
        expect(page.locator("#scheduledName")).to_be_editable()
        page.locator("#scheduledName").fill("产品群周报")
        page.locator("#scheduledAnalysisDays").select_option("7")
        page.locator("#scheduledKind").select_option("thursday")
        page.locator("#scheduledTime").fill("19:30")
        page.locator("#scheduledTargetChoices input").first.check()
        expect(page.locator('[data-scheduled-target-tab="private"]')).to_have_count(0)
        page.locator("#scheduledPreviewButton").click()
        expect(page.locator("#scheduledPreview")).to_be_visible()
        page.screenshot(path=str(tmp_path / "analysis-preview.png"))
        page.locator("#scheduledSaveDraft").click()
        row = page.locator("#scheduledTaskRows tr").filter(has_text="产品群周报")
        expect(row).to_contain_text("草稿")
        row.locator('[data-task-action="edit"]').first.click()
        page.locator("#scheduledTestButton").click()
        expect(page.locator("#modalBackdrop")).to_be_visible()
        page.locator("#modalConfirm").click()
        expect(page.locator("#modalBackdrop")).to_be_hidden()
        page.locator("#scheduledSaveEnabled").click()
        expect(row).to_contain_text("已启用")
        row.locator('[data-task-action="toggle"]').click()
        page.locator("#modalConfirm").click()
        expect(row).to_contain_text("已暂停")
        row.locator('[data-task-action="runs"]').click()
        expect(page.locator("#scheduledTaskRuns")).to_be_visible()
        page.locator("#scheduledRunsBack").click()
        row.locator('[data-task-action="delete"]').click()
        page.locator("#modalConfirm").click()
        expect(row).to_have_count(0)
        calls = page.evaluate("window.taskCalls")
        assert all(call["name"] != "save_feature" for call in calls)
        saved = next(call["args"][0] for call in calls if call["name"] == "save_scheduled_task")
        assert saved["action"] == "group_analysis" and saved["arguments"] == {"days": 7}
        assert saved["schedule"]["kind"] == "weekly" and saved["schedule"]["weekdays"] == [3]
        preview = next(call["args"][0] for call in calls if call["name"] == "preview_scheduled_task")
        assert preview["preview_target"] == {"type": "group", "id": saved["targets"][0]["id"]}
        assert not errors, errors
    finally:
        page.close()


@pytest.mark.parametrize("legacy_days", [1, 30])
def test_analysis_old_rules_require_explicit_migration_and_group_selection(browser, manager_url, legacy_days):
    page = browser.new_page()
    try:
        open_schedules(page, manager_url)
        page.evaluate("""(days) => {
          const getContext = window.pywebview.api.get_scheduled_task_context;
          window.pywebview.api.get_scheduled_task_context = async () => {
            const result = await getContext();
            result.data.legacy_group_analysis = {state: 'available', mode: 'blacklist',
              days, times: ['09:00', '21:00'], requires_target_selection: true,
              message: '旧排除名单需要重新选择日报群。'};
            return result;
          };
          window.pywebview.api.preview_group_analysis_migration = (payload) => {
            window.migrationPreview = payload;
            return {ok: true, data: {token: 'preview-token', account_id: payload.account_id,
              items: [{name: '旧日报', schedule: {kind: 'daily', time: '09:00'}, targets: payload.targets}],
              message: '迁移后保存为暂停草稿。'}};
          };
          window.pywebview.api.migrate_group_analysis = (token) => {
            window.migrationCommit = token;
            return {ok: true, data: {pending: true, message: '请关闭机器人微信后继续迁移。'}};
          };
        }""", legacy_days)
        page.locator("#refreshSchedules").click()
        expect(page.locator("#groupAnalysisMigration")).to_be_visible()
        page.locator("#previewGroupAnalysisMigration").click()
        assert page.evaluate("window.migrationPreview") is None
        page.locator("[data-analysis-migration-target]").first.check()
        if legacy_days > 7:
            expect(page.locator("#analysisMigrationDays")).to_have_value("")
            page.locator("#previewGroupAnalysisMigration").click()
            assert page.evaluate("window.migrationPreview") is None
            page.locator("#analysisMigrationDays").select_option("7")
        page.locator("#previewGroupAnalysisMigration").click()
        expect(page.locator("#modalBackdrop")).to_be_visible()
        assert page.evaluate("window.migrationCommit") is None
        page.locator("#modalConfirm").click()
        expect(page.locator("#modalBackdrop")).to_be_hidden()
        assert page.evaluate("window.migrationCommit") == "preview-token"
        assert page.evaluate("window.migrationPreview.targets[0].type") == "group"
        assert page.evaluate("window.migrationPreview.days") == (7 if legacy_days > 7 else legacy_days)
    finally:
        page.close()


@pytest.mark.parametrize("phase", ["confirmed", "prepared"])
def test_analysis_migration_resumes_after_stop_without_selecting_groups_again(browser, manager_url, phase):
    page = browser.new_page()
    try:
        open_schedules(page, manager_url)
        page.evaluate("""(phase) => {
          window.analysisMigrationPhase = phase;
          const getContext = window.pywebview.api.get_scheduled_task_context;
          window.pywebview.api.get_scheduled_task_context = async () => {
            const result = await getContext();
            Object.assign(result.data, {account_id: '', groups: [], runtime_ready: false,
              legacy_group_analysis: {state: window.analysisMigrationPhase, account_id: 'wxid_robot',
                mode: 'blacklist', times: ['09:00'], requires_target_selection: true,
                message: '停机已完成，可以继续已确认的迁移。'}});
            return result;
          };
          window.pywebview.api.preview_group_analysis_migration = (payload) => {
            window.migrationPreview = payload;
            return {ok: true, data: {token: 'resume-token', account_id: 'wxid_robot',
              items: [{name: '已确认日报', schedule: {kind: 'daily', time: '09:00'},
                targets: [{type: 'group', id: 'confirmed@chatroom'}]}]}};
          };
          window.pywebview.api.migrate_group_analysis = (token) => {
            window.migrationCommit = token;
            window.analysisMigrationPhase = 'complete';
            return {ok: true, data: {items: [], message: '迁移已完成。'}};
          };
        }""", phase)
        page.locator("#refreshSchedules").click()
        expect(page.locator("#previewGroupAnalysisMigration")).to_be_enabled()
        expect(page.locator("#previewGroupAnalysisMigration")).to_have_text("继续迁移")
        expect(page.locator("[data-analysis-migration-target]")).to_have_count(0)
        expect(page.locator("#cancelGroupAnalysisMigration")).to_have_count(1 if phase == "confirmed" else 0)
        page.locator("#previewGroupAnalysisMigration").click()
        expect(page.locator("#modalBackdrop")).to_be_visible()
        assert page.evaluate("window.migrationPreview") == {"account_id": "wxid_robot"}
        assert page.evaluate("window.migrationCommit") is None
        page.locator("#modalConfirm").click()
        expect(page.locator("#groupAnalysisMigration")).to_be_hidden()
        assert page.evaluate("window.migrationCommit") == "resume-token"
    finally:
        page.close()


def test_confirmed_analysis_migration_can_be_cancelled_without_starting_robot(browser, manager_url):
    page = browser.new_page()
    try:
        open_schedules(page, manager_url)
        page.evaluate("""() => {
          const getContext = window.pywebview.api.get_scheduled_task_context;
          window.pywebview.api.get_scheduled_task_context = async () => {
            const result = await getContext();
            Object.assign(result.data, {account_id: '', groups: [],
              legacy_group_analysis: {state: window.migrationCancelled ? 'none' : 'confirmed',
                account_id: 'wxid_robot'}});
            return result;
          };
          window.pywebview.api.cancel_group_analysis_migration = (account) => {
            window.migrationCancelled = account;
            return {ok: true, data: {cancelled: true}};
          };
          window.pywebview.api.start_robot = () => { throw new Error('Unexpected startup'); };
        }""")
        page.locator("#refreshSchedules").click()
        expect(page.locator("#cancelGroupAnalysisMigration")).to_be_enabled()
        page.locator("#cancelGroupAnalysisMigration").click()
        expect(page.locator("#modalBackdrop")).to_be_visible()
        assert page.evaluate("window.migrationCancelled") is None
        page.locator("#modalConfirm").click()
        expect(page.locator("#groupAnalysisMigration")).to_be_hidden()
        assert page.evaluate("window.migrationCancelled") == "wxid_robot"
    finally:
        page.close()


def test_analysis_migration_never_resumes_for_another_logged_in_account(browser, manager_url):
    page = browser.new_page()
    try:
        open_schedules(page, manager_url)
        page.evaluate("""() => {
          const getContext = window.pywebview.api.get_scheduled_task_context;
          window.pywebview.api.get_scheduled_task_context = async () => {
            const result = await getContext();
            Object.assign(result.data, {account_id: 'wxid_other',
              legacy_group_analysis: {state: 'prepared', account_id: 'wxid_robot'}});
            return result;
          };
        }""")
        page.locator("#refreshSchedules").click()
        expect(page.locator("#previewGroupAnalysisMigration")).not_to_be_visible()
    finally:
        page.close()
