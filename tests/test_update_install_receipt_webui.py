"""Accepted installs keep a single write while progress reads recover."""
import pytest
from playwright.sync_api import TimeoutError, expect

from test_manager_startup import browser, manager_url  # noqa: F401
from test_update_flow_webui import update_page, _download_and_wait_for_consent, _open_update_settings


def _hold_install_progress(page):
    page.evaluate("""() => {
      const api=window.pywebview.api, check=api.check_update;
      window.installStarted=false;
      window.installStatusFails=true;
      window.installStatusReads=0;
      api.check_update=()=>window.installStarted
        ? {ok:true,data:{busy:true}} : check();
      api.get_update_status=async()=>{
        if(window.installStarted) window.installStatusReads++;
        return window.installStarted && window.installStatusFails
          ? {ok:false,error:{message:'模拟安装后状态读取中断',recorded:true}}
          : {ok:true,data:window.testUpdateStatus};
      };
      api.install_update=async(...args)=>{
        window.updateCalls.install.push(args);
        if(window.installStarted)return {ok:true,data:{accepted:false,busy:true}};
        window.installStarted=true;
        window.updateCalls.effects.push('stop','install');
        window.testUpdateStatus={...window.testUpdateStatus,state:'running',phase:'stopping_services',
          confirmation_token:'',message:'正在关闭机器人及微信'};
        return {ok:true,data:{accepted:true}};
      };
    }""")


@pytest.mark.parametrize("width", [1380, 430])
def test_accepted_install_keeps_card_and_close_guard_when_progress_read_fails(update_page, tmp_path, width):
    page=update_page
    page.set_viewport_size({"width":width,"height":860})
    _open_update_settings(page)
    _download_and_wait_for_consent(page)
    _hold_install_progress(page)
    page.locator("#modalConfirm").click()
    expect(page.locator("#modalBackdrop")).to_be_hidden()
    expect(page.locator("#updateCard")).to_be_visible()
    expect(page.locator("#updateCardMessage")).to_contain_text("安装请求已接受")
    expect(page.locator("#updateCardAction")).to_be_hidden()
    assert page.evaluate("window.updateTestState.updateInstalling")
    assert page.evaluate("""() => {
      const event=new Event('beforeunload',{cancelable:true});
      dispatchEvent(event);return event.defaultPrevented;
    }""")
    page.evaluate("window.updateTestController.refreshUpdate(true)")
    expect(page.locator("#modalBackdrop")).to_be_hidden()
    expect(page.locator("#checkUpdate")).to_have_text("查看更新")
    expect(page.locator("#updateCardAction")).to_be_hidden()
    assert len(page.evaluate("window.updateCalls.install"))==1
    assert len(page.evaluate("window.updateCalls.download"))==1
    page.screenshot(path=str(tmp_path/f"accepted-install-read-failure-{width}.png"))
    page.evaluate("window.installStatusFails=false;window.updateTestController.pollUpdateStatus()")
    expect(page.locator("#updateCardMessage")).to_have_text("正在关闭机器人及微信")
    assert len(page.evaluate("window.updateCalls.install"))==1
    assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")


@pytest.mark.parametrize("section", ["model", "search"])
def test_accepted_install_blocks_settings_edits_during_unknown_progress(update_page, section):
    page=update_page
    _open_update_settings(page)
    page.locator(f'[data-settings-tab="{section}"]').click()
    if section=="model":
        page.locator('[data-provider-create]').click()
        field=page.locator('#providerModelInput')
    else:
        page.locator('label[for="webSearchEnabled"]').click()
        page.locator('#webSearchForm button[type="submit"]').click()
        expect(page.locator('#webSearchNote')).to_contain_text('已热更新')
        field=page.locator('#webSearchKey')
    _download_and_wait_for_consent(page)
    _hold_install_progress(page)
    page.locator('#modalConfirm').click()
    expect(page.locator('#modalBackdrop')).to_be_hidden()
    expect(page.locator('.app-shell')).to_have_js_property('inert',True)
    value=field.input_value()
    with pytest.raises(TimeoutError):
        field.click(timeout=500)
    page.keyboard.type('blocked-during-install')
    expect(field).to_have_value(value)
    assert len(page.evaluate('window.updateCalls.install'))==1


def test_accepted_install_close_failure_uses_fresh_close_consent_after_read_recovers(update_page):
    page=update_page
    _download_and_wait_for_consent(page)
    _hold_install_progress(page)
    page.locator("#modalConfirm").click()
    expect(page.locator("#modalBackdrop")).to_be_hidden()
    page.evaluate("""() => {
      window.testUpdateStatus={...window.testUpdateStatus,state:'ready',phase:'close_failed',
        close_pending:true,confirmation_token:'fresh-close-consent',
        error:{code:'UPD-1007',message:'管理器未能自动关闭，请再次关闭管理器。'}};
      window.installStatusFails=false;
      window.pywebview.api.install_update=async(...args)=>{
        window.updateCalls.install.push(args);
        window.testUpdateStatus={...window.testUpdateStatus,state:'restarting',phase:'restarting',
          close_pending:false,confirmation_token:'',error:null,message:'正在再次关闭管理器'};
        return {ok:true,data:{accepted:true}};
      };
      return window.updateTestController.pollUpdateStatus();
    }""")
    expect(page.locator("#modalTitle")).to_have_text("再次关闭管理器以继续更新？")
    page.locator("#modalCancel").click()
    page.locator("#updateCardAction").click()
    expect(page.locator("#modalConfirm")).to_have_text("再次关闭管理器")
    page.locator("#modalConfirm").click()
    expect(page.locator("#modalBackdrop")).to_be_hidden()
    assert page.evaluate("window.updateCalls.install")==[
        ["1.0.1","consent-release-first"],["1.0.1","fresh-close-consent"]]
    assert page.evaluate("window.updateCalls.effects")==["stop","install"]
    assert len(page.evaluate("window.updateCalls.download"))==1


def test_confirmed_install_failure_releases_close_guard_and_retains_retry(update_page):
    page=update_page
    _download_and_wait_for_consent(page)
    _hold_install_progress(page)
    page.locator("#modalConfirm").click()
    expect(page.locator("#modalBackdrop")).to_be_hidden()
    page.evaluate("""() => {
      window.testUpdateStatus={...window.testUpdateStatus,state:'failed',phase:'failed',
        error:{code:'UPD-1007',message:'更新安装程序未能启动，原版本文件未更改。'}};
      window.installStatusFails=false;
      return window.updateTestController.pollUpdateStatus();
    }""")
    expect(page.locator("#updateCardTitle")).to_have_text("更新未完成")
    expect(page.locator("#updateCardMessage")).to_contain_text("原版本文件未更改")
    expect(page.locator("#updateCardAction")).to_have_text("重试下载")
    assert not page.evaluate("window.updateTestState.updateInstalling")
    expect(page.locator('.app-shell')).to_have_js_property('inert',False)
    assert len(page.evaluate("window.updateCalls.install"))==1


def test_install_failure_does_not_unlock_an_open_onboarding_dialog(update_page):
    page=update_page
    _open_update_settings(page)
    page.evaluate("""() => {
      const api=window.pywebview.api, read=api.get_onboarding_readiness;
      api.get_onboarding_readiness=async()=>{
        const result=await read();
        return new Promise(resolve=>{window.finishOnboardingRead=()=>resolve(result);});
      };
    }""")
    page.locator('#openOnboarding').click()
    _download_and_wait_for_consent(page)
    _hold_install_progress(page)
    page.locator('#modalConfirm').click()
    expect(page.locator('#modalBackdrop')).to_be_hidden()
    page.evaluate('window.finishOnboardingRead()')
    expect(page.locator('#onboardingBackdrop')).to_be_visible()
    page.evaluate("""() => {
      window.testUpdateStatus={...window.testUpdateStatus,state:'failed',phase:'failed',
        error:{code:'UPD-1007',message:'安装未能启动'}};
      window.installStatusFails=false;
      return window.updateTestController.pollUpdateStatus();
    }""")
    assert not page.evaluate('window.updateTestState.updateInstalling')
    expect(page.locator('.app-shell')).to_have_js_property('inert',True)
    page.locator('#onboardingRecommended').click()
    expect(page.locator('#onboardingBackdrop')).to_be_hidden()
    expect(page.locator('.app-shell')).to_have_js_property('inert',False)


def test_status_read_started_before_install_cannot_restore_consumed_consent(update_page):
    page=update_page
    _download_and_wait_for_consent(page)
    _hold_install_progress(page)
    page.evaluate("""() => {
      const api=window.pywebview.api, read=api.get_update_status;
      window.staleInstallStatus=structuredClone(window.testUpdateStatus);
      let first=true;
      api.get_update_status=()=>{
        if(!first)return read();first=false;
        return new Promise(resolve=>{window.finishPreInstallRead=resolve;});
      };
      window.preInstallPoll=window.updateTestController.pollUpdateStatus();
    }""")
    page.locator("#modalConfirm").click()
    expect(page.locator("#modalBackdrop")).to_be_hidden()
    page.evaluate("""async () => {
      window.finishPreInstallRead({ok:true,data:window.staleInstallStatus});
      await window.preInstallPoll;
    }""")
    expect(page.locator("#updateCardAction")).to_be_hidden()
    expect(page.locator("#modalBackdrop")).to_be_hidden()
    assert page.evaluate("window.updateTestState.updateInstalling")
    assert len(page.evaluate("window.updateCalls.install"))==1
