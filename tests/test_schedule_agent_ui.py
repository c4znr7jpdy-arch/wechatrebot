"""Account-scoped AI task UI; synthetic preview data, no robot.

Browser plugin not available: reuse the repository's Playwright preview server.
"""
from pathlib import Path

import pytest
from playwright.sync_api import expect

from test_manager_startup import browser, manager_url  # noqa: F401


def open_agent_tasks(browser, manager_url, *, width=1440, theme="dark"):
    page = browser.new_page(viewport={"width": width, "height": 1000})
    page.add_init_script("""(() => {
      localStorage.setItem('wechatrebot-theme', 'dark');
      Object.defineProperty(window, 'createScheduledTasks', {configurable:true, set(create) {
        Object.defineProperty(window, 'createScheduledTasks', {value:helpers => {
          window.agentTaskState=helpers.state;
          window.agentTaskController=create(helpers);
          return window.agentTaskController;
        }});
      }});
    })();""")
    page.goto(manager_url, wait_until="domcontentloaded")
    expect(page.locator("#stateTitle")).to_have_text("机器人可用")
    if page.locator("#onboardingRecommended").is_visible():
        page.locator("#onboardingRecommended").click()
    page.evaluate("""() => {
      const api=window.pywebview.api, read=api.get_scheduled_task_context, status=api.get_status;
      const account='wxid_demo_account', group='group-agent@chatroom';
      window.nativeTask={id:'agent-weather',account_id:account,action:'agent',name:'青岛每日天气预报',
        description:'每天查询青岛当天天气并发送到当前群聊。',targets:[{type:'group',id:group,name:group}],
        enabled:true,draft:false,status:'enabled',schedule_text:'每天 08:30',cron_expression:'30 8 * * *',
        next_run_time:'2026-11-14T00:30:00+00:00',last_run_at:null,last_result:''};
      api.get_scheduled_task_context=async lightweight => {
        const result=await read(lightweight);
        result.data={...result.data,account_id:account,items:[],loading:Boolean(lightweight),
          runtime_ready:!lightweight,
          groups:lightweight ? [] : [{type:'group',id:group,name:'天气通知群'}],contacts:[]};
        return result;
      };
      api.get_schedules=async () => {
        if (window.agentReadGate) {window.agentReadWaiting=true; await window.agentReadGate;}
        return {ok:true,data:{account_id:account,items:[],
          agent_items:[...(window.deletedAgent ? [] : [window.nativeTask]),{...window.nativeTask,id:'other-agent',account_id:'wxid_other',name:'其他账号任务'},
            {...window.nativeTask,id:'old-agent',account_id:'',name:'无账号旧任务'}]}};
      };
      api.get_legacy_schedule_migrations=async () => ({ok:true,data:{items:[]}});
      api.set_schedule_enabled=async (id, enabled) => {
        window.nativeTask={...window.nativeTask,enabled,status:enabled ? 'enabled' : 'paused'};
        return {ok:true,data:{updated:true}};
      };
      api.update_schedule_time=async (id, time) => {
        window.nativeTask={...window.nativeTask,schedule_text:`每天 ${time}`,cron_expression:`${time.slice(3)} ${time.slice(0,2)} * * *`};
        return {ok:true,data:{updated:true}};
      };
      api.delete_schedule=async id => {
        window.deletedAgent=id;
        return {ok:true,data:{deleted:true}};
      };
      api.get_status=async () => {
        const result=structuredClone(await status());
        if (window.agentAccountChanged) result.data.runtime.wechat_identity='wxid_other';
        if (window.agentAccountUnknown) result.data.runtime.account_status_available=false;
        return result;
      };
    }""")
    page.locator('.nav-item[data-page="schedules"]').click()
    expect(page.locator("#scheduledTaskRows")).to_contain_text("青岛每日天气预报")
    if theme == "light":
        page.locator("#themeToggle").click()
    return page


@pytest.mark.parametrize("width,theme", [(1440,"dark"),(1080,"light")])
def test_agent_tasks_share_the_list_and_only_show_current_account(browser, manager_url, tmp_path: Path, width, theme):
    page = open_agent_tasks(browser, manager_url, width=width, theme=theme)
    errors=[]
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        expect(page.locator("#scheduledAccountLabel")).to_have_text("当前微信账号：wxid_demo_account")
        expect(page.locator("#scheduleTotal")).to_have_text("1")
        expect(page.locator("#scheduledTaskRows")).to_contain_text("天气通知群")
        expect(page.locator("#scheduledTaskRows")).not_to_contain_text("其他账号任务")
        expect(page.locator('#scheduledTaskRows [data-task-action="copy"]')).to_have_count(0)
        expect(page.locator("#scheduledTaskRows")).not_to_contain_text("无账号旧任务")
        page.screenshot(path=str(tmp_path / f"agent-list-{theme}.png"))
        page.locator('[data-task-action="runs"][data-id="agent-weather"]').first.click()
        expect(page.locator("#scheduledRunsBody")).to_contain_text("每天查询青岛当天天气")
        expect(page.locator("#scheduledRunsBody")).to_contain_text("最近执行：—")
        page.locator("#scheduledRunsBack").click()
        page.locator('[data-task-action="time"][data-id="agent-weather"]').click()
        expect(page.locator("#modalTitle")).to_have_text("修改执行时间")
        expect(page.locator("#modalInput")).to_have_value("08:30")
        page.locator("#modalInput").fill("09:15")
        page.locator("#modalConfirm").click()
        weather=page.locator("#scheduledTaskRows tr").filter(has_text="青岛每日天气预报")
        expect(weather).to_contain_text("每天 09:15")
        weather.locator('[data-task-action="toggle"]').click()
        expect(weather.locator('[data-task-action="toggle"]')).to_have_text("启用")
        weather.locator('[data-task-action="toggle"]').click()
        expect(weather.locator('[data-task-action="toggle"]')).to_have_text("暂停")
        page.screenshot(path=str(tmp_path / f"agent-updated-{theme}.png"))
        weather.locator('[data-task-action="delete"]').click()
        page.locator("#modalConfirm").click()
        expect(page.locator("#scheduledTaskRows")).not_to_contain_text("青岛每日天气预报")
        assert page.url == manager_url and page.title().startswith("定时任务")
        assert not errors, errors
        print(f"AI task UI evidence: {tmp_path}")
    finally:
        page.close()


def test_agent_task_account_change_discards_pending_response_and_unknown_hides_rows(browser, manager_url, tmp_path: Path):
    page=open_agent_tasks(browser, manager_url)
    try:
        page.evaluate("""() => {
          window.agentReadGate=new Promise(resolve => {window.releaseAgentRead=resolve;});
          window.agentTaskController.loadSchedules({background:true});
        }""")
        page.wait_for_function("window.agentReadWaiting === true")
        page.evaluate("window.agentAccountChanged=true")
        expect(page.locator("#scheduledTaskRows [data-task-action]")).to_have_count(0)
        page.evaluate("window.releaseAgentRead()")
        expect(page.locator("#scheduledTaskRows [data-task-action]")).to_have_count(0)
        assert page.evaluate("window.agentTaskState.agentSchedules.length") == 0
        page.screenshot(path=str(tmp_path / "agent-account-changed.png"))
    finally:
        page.close()
    page=open_agent_tasks(browser, manager_url)
    try:
        page.locator('[data-task-action="runs"][data-id="agent-weather"]').first.click()
        page.evaluate("window.agentAccountUnknown=true")
        page.wait_for_function("() => window.agentTaskState.status.runtime.account_status_available === false")
        expect(page.locator("#scheduledTaskRows [data-task-action]")).to_have_count(0)
        expect(page.locator("#scheduledTaskRuns")).to_be_hidden()
    finally:
        page.close()


def test_one_time_agent_task_keeps_explanation_and_state_actions_without_recurring_time_editor(browser, manager_url):
    page=open_agent_tasks(browser, manager_url)
    try:
        page.evaluate("""async () => {
          window.nativeTask={...window.nativeTask,cron_expression:'',schedule_text:'2026-11-14 08:30，仅一次'};
          await window.agentTaskController.loadSchedules();
        }""")
        row=page.locator("#scheduledTaskRows tr").filter(has_text="青岛每日天气预报")
        expect(row).to_contain_text("仅一次")
        expect(row.locator('[data-task-action="time"]')).to_have_count(0)
        expect(row.locator('[data-task-action="toggle"]')).to_be_enabled()
        expect(row.locator('[data-task-action="delete"]')).to_be_enabled()
        row.locator('[data-task-action="runs"]').last.click()
        expect(page.locator("#scheduledRunsBody")).to_contain_text("2026-11-14 08:30，仅一次")
    finally:
        page.close()
