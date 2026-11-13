"""Regression coverage for tray return and message-kind navigation; no robot starts."""
import pytest
from playwright.sync_api import expect
from test_manager_startup import browser, manager_url  # noqa: F401


@pytest.mark.parametrize("resume", ["focus", "visibilitychange", "paired"])
@pytest.mark.parametrize("older_page", [False, True])
def test_resume_after_three_minutes_preserves_overlapping_message_nodes(browser, manager_url, resume, older_page):
    page = browser.new_page()
    try:
        page.add_init_script("""(() => {
          const interval = window.setInterval;
          window.setInterval = (fn, ...args) => ['pollHistory','refreshStatus'].includes(fn.name) ? 0 : interval(fn,...args);
        })();""")
        page.goto(manager_url)
        expect(page.locator('#stateTitle')).to_have_text('机器人可用')
        if page.locator('#onboardingRecommended').is_visible():
            page.locator('#onboardingRecommended').click()
        page.evaluate("""() => {
          window.latest = 40; window.directoryRefreshes = [];
          window.pywebview.api.get_conversations = async filters => {
            window.directoryRefreshes.push(filters.refresh_groups);
            return {ok:true,data:{account_id:'wxid_demo_account',items:[{conversation_id:'retained',
              display_name:'保留消息',type:'group',message_count:window.latest,last_timestamp:window.latest}],summary:{}}};
          };
          window.pywebview.api.get_messages = async (id, filters) => {
            const last = filters.before ? filters.before.id - 1 : window.latest;
            return {ok:true,data:{account_id:'wxid_demo_account',has_more:last > 20,
              items:Array.from({length:20},(_,i)=>({id:last-19+i,timestamp:1793200000+last-19+i,
                content:'记录 '+(last-19+i),role:'user',sender_id:'member',sender_name:'成员'}))}};
          };
        }""")
        page.locator('[data-page="history"].nav-item').click()
        expect(page.locator('#messageList article')).to_have_count(20)
        if older_page:
            page.locator('#messageList').evaluate('(list) => { list.scrollTop = 0; list.dispatchEvent(new Event("scroll")); }')
            expect(page.locator('#messageList article')).to_have_count(40)
        before = page.evaluate("""() => {
          const list = document.querySelector('#messageList');
          list.scrollTop = 180;
          window.oldRows = [...list.querySelectorAll('article')];
          const now = Date.now; Date.now = () => now()+181000;
          window.latest = 41;
          return {reads:directoryRefreshes.length, scroll:list.scrollTop};
        }""")
        page.evaluate("""event => {
          if(event === 'paired') {
            window.dispatchEvent(new Event('focus'));
            document.dispatchEvent(new Event('visibilitychange'));
          } else (event === 'focus' ? window : document).dispatchEvent(new Event(event));
        }""", resume)
        expect(page.locator('#messageList article')).to_have_count(41 if older_page else 21)
        assert page.evaluate("window.oldRows.every(row => row.isConnected)"), 'Background refresh rebuilt existing message rows'
        assert page.locator('#messageList').evaluate('(list) => list.scrollTop') == before['scroll']
        assert page.evaluate('window.directoryRefreshes.length') == before['reads'] + 1
        assert page.evaluate('window.directoryRefreshes.at(-1)') is False
    finally:
        page.close()


def test_kind_tabs_restore_selection_without_replaying_layout_animation(browser, manager_url):
    page = browser.new_page()
    try:
        page.add_init_script("""(() => {
          const interval = window.setInterval;
          window.setInterval = (fn,...args) => ['pollHistory','refreshStatus'].includes(fn.name) ? 0 : interval(fn,...args);
        })();""")
        page.goto(manager_url)
        expect(page.locator('#stateTitle')).to_have_text('机器人可用')
        if page.locator('#onboardingRecommended').is_visible():
            page.locator('#onboardingRecommended').click()
        page.evaluate("""() => {
          window.directoryReads=0; window.messageReads=0; window.layoutEnters=0;
          window.pywebview.api.get_conversations=async()=>{ window.directoryReads++; return {ok:true,data:{account_id:'wxid_demo_account',summary:{},
            items:[['p','private'],['g1','group'],['g2','group']].map(([id,type])=>({conversation_id:id,type,display_name:id,message_count:1,last_timestamp:1}))}}; };
          window.pywebview.api.get_messages=async id=>{window.messageReads++;return {ok:true,data:{account_id:'wxid_demo_account',items:[{id:1,timestamp:1,content:id,role:'user',sender_id:'m'}],has_more:false}};};
          const enter=window.managerMotion.enter;
          window.managerMotion.enter=(node,...args)=>{if(node?.classList.contains('history-layout'))window.layoutEnters++;return enter(node,...args);};
        }""")
        page.locator('[data-page="history"].nav-item').click()
        expect(page.locator('#messageList .message-bubble')).to_have_text('p')
        page.locator('#historyKinds [data-kind="group"]').click()
        page.locator('[data-conversation="g2"]').click()
        expect(page.locator('#messageList .message-bubble')).to_have_text('g2')
        page.locator('#historyKinds [data-kind="private"]').click()
        expect(page.locator('#messageList .message-bubble')).to_have_text('p')
        before=page.evaluate('({directory:directoryReads,messages:messageReads,animations:layoutEnters})')
        page.locator('#historyKinds [data-kind="group"]').click()
        expect(page.locator('[data-conversation="g2"]')).to_have_class('conversation-item active')
        expect(page.locator('#messageList .message-bubble')).to_have_text('g2')
        assert page.evaluate('directoryReads') == before['directory']
        assert page.evaluate('messageReads') == before['messages']
        assert page.evaluate('layoutEnters') == 0
    finally:
        page.close()
