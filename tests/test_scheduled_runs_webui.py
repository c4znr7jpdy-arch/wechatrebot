"""Execution records stay with the selected task and current WeChat account."""
import pytest
from playwright.sync_api import expect

from test_manager_startup import browser, manager_url  # noqa: F401


def _open_runs_page(browser, manager_url, *, width=1380):
    page = browser.new_page(viewport={"width": width, "height": 860})
    page.add_init_script("""(() => {
      Object.defineProperty(window, 'createScheduledTasks', {configurable:true, set(create) {
        Object.defineProperty(window, 'createScheduledTasks', {value:helpers => {
          window.runReviewState=helpers.state;
          return create(helpers);
        }});
      }});
    })();""")
    page.goto(manager_url)
    expect(page.locator("#stateTitle")).to_have_text("机器人可用")
    if page.locator("#onboardingRecommended").is_visible():
        page.locator("#onboardingRecommended").click()
    page.evaluate("""() => {
      const api=window.pywebview.api, context=api.get_scheduled_task_context, status=api.get_status;
      window.runAccount='wxid_demo_account';
      window.runAccountAvailable=true;
      api.get_status=async () => {
        const result=await status();
        result.data.runtime.wechat_identity=window.runAccount;
        result.data.runtime.account_status_available=window.runAccountAvailable;
        // Preview omits this field, so transient uncertainty retains the
        // last account identity in the normal shell path.
        delete result.data.history_account;
        setTimeout(() => {
          document.body.dataset.reviewAccountAvailable=String(
            window.runReviewState.status.runtime.account_status_available);
        }, 0);
        return result;
      };
      api.get_scheduled_task_context=async (...args) => {
        const result=await context(...args);
        result.data.account_id=window.runAccount;
        result.data.items=window.runAccount==='wxid_demo_account' ? ['a','b'].map(id=>({
          id, account_id:result.data.account_id, revision:1, name:`任务${id.toUpperCase()}`,
          action:'text', arguments:{text:id}, targets:[{type:'group',id:'probe-group',name:'测试群'}],
          schedule:{kind:'daily',time:'08:00'}, enabled:false, draft:true, status:'draft'})) : [];
        return result;
      };
      window.runReads=[];
      window.currentRunResponse={ok:true,data:{items:[{status:'submitted',message:'B的执行记录',
        deliveries:[{name:'B的对象',status:'submitted'}]}]}};
      api.get_scheduled_task_runs=(account,id) => {
        window.runReads.push({account,id});
        if (id==='a') return new Promise(resolve => {window.finishOldRuns=resolve;});
        return Promise.resolve(structuredClone(window.currentRunResponse));
      };
    }""")
    page.locator('.nav-item[data-page="schedules"]').click()
    expect(page.locator('#scheduledTaskRows [data-task-action="runs"]')).to_have_count(2)
    return page


def _finish_old_read(page, outcome):
    page.evaluate("""outcome => {
      window.finishOldRuns(outcome==='success'
        ? {ok:true,data:{items:[{status:'failed',message:'A的旧执行记录'}]}}
        : {ok:false,error:{message:'A的旧读取失败'}});
    }""", outcome)
    # Let the synthetic response pass through both the API adapter and renderer.
    page.wait_for_timeout(80)


@pytest.mark.parametrize("old_outcome", ["success", "failure"])
def test_old_run_response_does_not_replace_new_task(browser, manager_url, tmp_path, old_outcome):
    page = _open_runs_page(browser, manager_url)
    try:
        page.locator('[data-task-action="runs"][data-id="a"]').click()
        page.wait_for_function("window.runReads.length===1")
        page.locator("#scheduledRunsBack").click()
        page.locator('[data-task-action="runs"][data-id="b"]').click()
        expect(page.locator("#scheduledRunsBody")).to_contain_text("B的执行记录")
        _finish_old_read(page, old_outcome)
        expect(page.locator("#scheduledTaskRuns h2")).to_contain_text("任务B")
        expect(page.locator("#scheduledRunsBody")).to_contain_text("B的执行记录")
        expect(page.locator("#scheduledRunsBody")).not_to_contain_text("A的旧")
        page.screenshot(path=str(tmp_path / f"old-run-{old_outcome}.png"))
    finally:
        page.close()


@pytest.mark.parametrize("phase", ["pending", "loaded"])
@pytest.mark.parametrize("account", ["wxid_demo_replacement", ""])
def test_account_change_clears_previous_execution_records(browser, manager_url, phase, account):
    page = _open_runs_page(browser, manager_url)
    try:
        task = "a" if phase == "pending" else "b"
        page.locator(f'[data-task-action="runs"][data-id="{task}"]').click()
        page.wait_for_function("window.runReads.length===1")
        if phase == "loaded":
            expect(page.locator("#scheduledRunsBody")).to_contain_text("B的执行记录")
        page.evaluate("account => {window.runAccount=account;}", account)
        expect(page.locator("#scheduledTaskRows [data-task-action='runs']")).to_have_count(0)
        if phase == "pending":
            _finish_old_read(page, "success")
        expect(page.locator("#scheduledTaskRuns")).to_be_hidden()
        expect(page.locator("#scheduledTaskRuns")).to_be_empty()
        expect(page.locator("#scheduleListView")).to_be_visible()
        expect(page.locator("#scheduleHeading")).to_have_text("定时任务")
    finally:
        page.close()


@pytest.mark.parametrize("phase", ["pending", "loaded"])
def test_transient_account_uncertainty_keeps_same_account_records(browser, manager_url, phase):
    page = _open_runs_page(browser, manager_url)
    try:
        task = "a" if phase == "pending" else "b"
        page.locator(f'[data-task-action="runs"][data-id="{task}"]').click()
        page.wait_for_function("window.runReads.length===1")
        if phase == "loaded":
            expect(page.locator("#scheduledRunsBody")).to_contain_text("B的执行记录")
        page.evaluate("window.runAccountAvailable=false")
        expect(page.locator("body")).to_have_attribute("data-review-account-available", "false")
        if phase == "pending":
            _finish_old_read(page, "success")
        expect(page.locator("#scheduledTaskRuns")).to_be_visible()
        expect(page.locator("#scheduledRunsBody")).to_contain_text(
            "A的旧执行记录" if phase == "pending" else "B的执行记录"
        )
        assert page.evaluate("window.runReads.length") == 1
    finally:
        page.close()


@pytest.mark.parametrize("width", [1380, 430])
def test_current_record_failure_can_return_and_retry(browser, manager_url, tmp_path, width):
    page = _open_runs_page(browser, manager_url, width=width)
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        page.evaluate("window.currentRunResponse={ok:false,error:{message:'当前记录读取失败，请稍后重试。'}}")
        page.locator('[data-task-action="runs"][data-id="b"]').click()
        expect(page.locator("#scheduledRunsBody")).to_contain_text("当前记录读取失败")
        page.locator("#scheduledRunsBack").click()
        page.evaluate("window.currentRunResponse={ok:true,data:{items:[]}}")
        page.locator('[data-task-action="runs"][data-id="b"]').click()
        expect(page.locator("#scheduledRunsBody")).to_contain_text("尚无执行记录")
        expect(page.locator("#scheduledRunsBack")).to_be_visible()
        assert page.url == manager_url and page.title().startswith("定时任务")
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        assert not errors, errors
        page.screenshot(path=str(tmp_path / f"current-record-retry-{width}.png"))
    finally:
        page.close()
