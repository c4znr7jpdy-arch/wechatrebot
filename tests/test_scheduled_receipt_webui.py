"""Keep confirmed task results visible when the following list read fails."""
import pytest
from playwright.sync_api import expect

from test_manager_startup import browser, manager_url  # noqa: F401
from test_scheduled_analysis_ui import open_schedules


@pytest.mark.parametrize('width', [1380, 430])
def test_saved_task_remains_visible_when_context_readback_fails(browser, manager_url, tmp_path, width):
    page = browser.new_page(viewport={'width': width, 'height': 920})
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        open_schedules(page, manager_url)
        page.locator('#newScheduledTask').click()
        page.locator('#scheduledName').fill('已经确认保存的任务')
        page.evaluate("""() => {
          const api = window.pywebview.api, save = api.save_scheduled_task;
          const context = api.get_scheduled_task_context;
          window.taskSaved = false;
          window.taskReadFails = true;
          api.save_scheduled_task = async (...args) => {
            const result = await save(...args);
            window.taskReceipt = structuredClone(result.data.item);
            window.taskSaved = true;
            return result;
          };
          api.get_scheduled_task_context = (...args) => window.taskSaved && window.taskReadFails
            ? {ok:false,error:{message:'模拟保存后的列表读取失败',recorded:true}}
            : context(...args);
        }""")
        page.locator('#scheduledSaveDraft').click()
        expect(page.locator('#scheduledTaskEditor')).to_be_hidden()
        expect(page.locator('#scheduledTaskRows')).to_contain_text('已经确认保存的任务')
        expect(page.locator('#scheduleContextMessage')).to_contain_text('刷新')
        assert page.evaluate("window.taskCalls.filter(call => call.name === 'save_scheduled_task').length") == 1
        page.screenshot(path=str(tmp_path / 'scheduled-confirmed-read-failed.png'), animations='disabled')
        page.locator('#refreshSchedules').click()
        expect(page.locator('#refreshSchedules')).to_be_enabled()
        expect(page.locator('#scheduledTaskRows')).to_contain_text('已经确认保存的任务')
        expect(page.locator('#scheduleContextMessage')).to_contain_text('无需重复提交')
        page.evaluate('window.taskReadFails = false')
        page.locator('#refreshSchedules').click()
        expect(page.locator('#refreshSchedules')).to_be_enabled()
        expect(page.locator('#scheduleContextMessage')).to_be_hidden()
        expect(page.locator('#scheduledTaskRows')).to_contain_text('已经确认保存的任务')
        assert page.evaluate("window.taskCalls.filter(call => call.name === 'save_scheduled_task').length") == 1
        assert page.url == manager_url and page.title()
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        assert not errors, errors
    finally:
        page.close()


@pytest.mark.parametrize('action', ['toggle', 'delete'])
def test_task_state_receipt_survives_readback_failure(browser, manager_url, action):
    page = browser.new_page()
    try:
        open_schedules(page, manager_url)
        for name in ['保留的任务', '操作的任务']:
            page.locator('#newScheduledTask').click()
            page.locator('#scheduledAction').select_option('text')
            page.locator('#scheduledName').fill(name)
            page.locator('#scheduledArg-text').fill('本地预览文字')
            page.locator('#scheduledTargetChoices input').first.check()
            page.locator('#scheduledSaveEnabled').click()
            expect(page.locator('#scheduledTaskEditor')).to_be_hidden()
        page.evaluate("""() => {
          window.pywebview.api.get_scheduled_task_context = () =>
            ({ok:false,error:{message:'模拟列表读取失败',recorded:true}});
        }""")
        row = page.locator('#scheduledTaskRows tr').filter(has_text='操作的任务')
        row.locator(f'[data-task-action="{action}"]').click()
        page.locator('#modalConfirm').click()
        expect(page.locator('#modalBackdrop')).to_be_hidden()
        expect(page.locator('#scheduledTaskRows')).to_contain_text('保留的任务')
        if action == 'delete':
            expect(row).to_have_count(0)
        else:
            expect(row.locator('.task-status')).to_have_text('已暂停')
        expect(page.locator('#scheduleContextMessage')).to_contain_text('无需重复提交')
    finally:
        page.close()


@pytest.mark.parametrize('failure', ['wrong_account', 'wrong_id', 'missing_revision'])
def test_invalid_task_receipt_does_not_discard_editor(browser, manager_url, failure):
    page = browser.new_page()
    try:
        open_schedules(page, manager_url)
        page.locator('#newScheduledTask').click()
        page.locator('#scheduledName').fill('需要核对保存结果的任务')
        page.evaluate("""failure => {
          const api = window.pywebview.api, save = api.save_scheduled_task;
          api.save_scheduled_task = async (...args) => {
            const result = await save(...args);
            const item = result.data.item;
            if (failure === 'wrong_account') item.account_id = 'other-account';
            if (failure === 'wrong_id') item.id = 'other-task';
            if (failure === 'missing_revision') delete item.revision;
            return result;
          };
        }""", failure)
        page.locator('#scheduledSaveDraft').click()
        expect(page.locator('#scheduledTaskEditor')).to_be_visible()
        expect(page.locator('#scheduledName')).to_have_value('需要核对保存结果的任务')
        expect(page.locator('#scheduledTaskError')).to_contain_text('未收到当前任务的操作确认')
    finally:
        page.close()


def test_list_read_started_before_save_cannot_remove_confirmed_task(browser, manager_url):
    page = browser.new_page()
    try:
        open_schedules(page, manager_url)
        page.locator('#newScheduledTask').click()
        page.locator('#scheduledName').fill('读取期间保存的任务')
        page.evaluate("""() => {
          const api = window.pywebview.api, read = api.get_scheduled_task_context;
          api.get_scheduled_task_context = async (...args) => {
            if (window.finishOldTaskList)
              return {ok:false,error:{message:'后续读取暂不可用',recorded:true}};
            const result = await read(...args);
            return new Promise(resolve => { window.finishOldTaskList = () => resolve(result); });
          };
          window.dispatchEvent(new Event('focus'));
        }""")
        page.wait_for_function("typeof window.finishOldTaskList === 'function'")
        page.locator('#scheduledSaveDraft').click()
        expect(page.locator('#scheduledTaskEditor')).to_be_hidden()
        expect(page.locator('#scheduledTaskRows')).to_contain_text('读取期间保存的任务')
        page.evaluate('window.finishOldTaskList()')
        expect(page.locator('#scheduledTaskRows')).to_contain_text('读取期间保存的任务')
        expect(page.locator('#scheduleContextMessage')).to_contain_text('无需重复提交')
    finally:
        page.close()


def test_late_save_receipt_does_not_clear_editor_after_account_change(browser, manager_url):
    page = browser.new_page()
    page.add_init_script("""(() => {
      Object.defineProperty(window, 'createScheduledTasks', {configurable:true,set(create) {
        Object.defineProperty(window, 'createScheduledTasks', {value: helpers => {
          window.taskState = helpers.state;
          return window.taskController = create(helpers);
        }});
      }});
    })();""")
    try:
        open_schedules(page, manager_url)
        page.locator('#newScheduledTask').click()
        page.locator('#scheduledName').fill('原账号尚待确认的任务')
        page.evaluate("""() => {
          const api = window.pywebview.api, save = api.save_scheduled_task;
          api.save_scheduled_task = async (...args) => {
            const result = await save(...args);
            return new Promise(resolve => { window.finishOldTaskSave = () => resolve(result); });
          };
        }""")
        page.locator('#scheduledSaveDraft').click()
        page.wait_for_function("typeof window.finishOldTaskSave === 'function'")
        page.evaluate("""() => {
          window.taskController.invalidateScheduledContext();
          window.taskState.historyAccount = 'second-account';
          window.taskState.scheduledContext = {account_id:'second-account',items:[]};
          window.finishOldTaskSave();
        }""")
        expect(page.locator('#scheduledSaveDraft')).to_be_disabled()
        expect(page.locator('#scheduledName')).to_have_value('原账号尚待确认的任务')
        expect(page.locator('#scheduledTaskEditor')).to_be_visible()
        assert page.evaluate('window.taskState.scheduledDirty') is True
        assert page.evaluate('window.taskState.scheduledContext.items') == []
    finally:
        page.close()
