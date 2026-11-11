"""Exercise startup through the delivered asset routes without starting a robot."""
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest
from playwright.sync_api import expect, sync_playwright

from wechatrebot_manager.app_shell import _asset_root
from wechatrebot_manager.plugin_center import _handler


@pytest.fixture(scope="module")
def manager_url(tmp_path_factory):
    service = SimpleNamespace(install_root=tmp_path_factory.mktemp("manager-install"))
    class PreviewHandler(_handler(service, manager_assets=_asset_root())):
        # Hundreds of isolated browser contexts cannot share their HTTP cache.
        # All asset responses have Content-Length; keep-alive avoids creating a
        # Windows TIME_WAIT socket for every stylesheet, script and image.
        protocol_version = "HTTP/1.1"

    server = ThreadingHTTPServer(("127.0.0.1", 0), PreviewHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        # The optional QR preview fixture is not part of the installed assets.
        yield f"http://127.0.0.1:{server.server_port}/?preview=1&contact=unavailable"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.fixture(scope="module")
def browser(manager_browser_launch_options):
    with sync_playwright() as runtime:
        instance = runtime.chromium.launch(**manager_browser_launch_options)
        try:
            yield instance
        finally:
            try:
                for context in instance.contexts:
                    context.close()
            finally:
                instance.close()


@pytest.mark.parametrize("viewport", [{"width": 1380, "height": 860}, {"width": 1080, "height": 680}])
def test_manager_loads_all_resources_and_opens_subscription(browser, manager_url, viewport, tmp_path: Path):
    page = browser.new_page(viewport=viewport)
    errors, failed_responses = [], []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
    page.on("response", lambda response: failed_responses.append(response.url) if response.status >= 400 else None)
    try:
        page.goto(manager_url)
        expect(page.locator("#versionText")).not_to_have_text("v0.0.0")
        expect(page.locator("#stateTitle")).not_to_have_text("正在检查环境")
        expect(page.locator("#globalRefresh")).to_have_count(0)
        if page.locator("#onboardingRecommended").is_visible():
            page.locator("#onboardingRecommended").click()
        page.locator('.nav-item[data-page="subscription"]').click()
        expect(page.locator("#page-subscription")).to_be_visible()
        page.screenshot(path=str(tmp_path / "subscription.png"))
        page.locator('.nav-item[data-page="activity"]').click()
        expect(page.locator("#page-activity")).to_be_visible()
        expect(page.locator("#activityList .diagnostic-card").first).to_be_visible()
        expect(page.locator("#activityList .diagnostic-card.info")).to_have_count(0)
        page.locator("#activityLevel").select_option("warning")
        expect(page.locator("#activityList .diagnostic-card.warning").first).to_be_visible()
        expect(page.locator("#activityList .diagnostic-card.error")).to_have_count(0)
        page.locator("#activityList details").first.locator("summary").click()
        expect(page.locator("#activityList .diagnostic-report").first).to_be_visible()
        page.screenshot(path=str(tmp_path / "activity.png"))
        page.locator('.nav-item[data-page="home"]').click()
        expect(page.locator("#page-home")).to_be_visible()
        expect(page.locator("#stateTitle")).to_have_text("机器人可用")
        page.screenshot(path=str(tmp_path / "home.png"))
        assert not failed_responses, failed_responses
        assert not errors, errors
        assert page.url == manager_url
        assert page.title()
    finally:
        page.close()


def test_wechat_signal_uses_live_connection_instead_of_last_environment_scan(browser, manager_url):
    page = browser.new_page()
    try:
        page.goto(manager_url)
        expect(page.locator("#versionText")).not_to_have_text("v0.0.0")
        if page.locator("#onboardingRecommended").is_visible():
            page.locator("#onboardingRecommended").click()
        page.evaluate("""() => {
          const api = window.pywebview.api, read = api.get_status;
          window.testConnection = 'connected';
          api.get_status = async () => {
            const result = await read();
            result.data.environment.process_running = window.testConnection !== 'connected';
            result.data.runtime = {...result.data.runtime,
              account_status_available: window.testConnection !== 'unknown',
              hook_ready: window.testConnection === 'connected',
              wechat_identity: window.testConnection === 'connected' ? 'wxid_current' : ''};
            return result;
          };
        }""")
        for state, label in [("connected", "已连接"), ("disconnected", "待连接"), ("unknown", "状态待确认")]:
            page.evaluate("state => { window.testConnection = state; }", state)
            # The existing status poll must update the signal without a refresh button.
            expect(page.locator("#signalWechatDetail")).to_have_text(f"4.1.8.27 · {label}")
    finally:
        page.close()


def test_download_connection_check_recovers_without_login_prompt(browser, manager_url, tmp_path):
    page = browser.new_page(viewport={"width": 1380, "height": 860})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
    try:
        page.goto(manager_url)
        expect(page.locator("#stateTitle")).to_have_text("机器人可用")
        if page.locator("#onboardingRecommended").is_visible():
            page.locator("#onboardingRecommended").click()
        page.evaluate("""() => {
          const api = window.pywebview.api, read = api.get_status;
          window.testDownloadPhase = 'unknown';
          api.get_status = async () => {
            const result = await read(), status = result.data;
            const phase = window.testDownloadPhase;
            status.online = phase === 'recovered';
            status.state = phase === 'unknown' ? 'VERIFYING_CONNECTION'
              : phase === 'logout' ? 'WAITING_WECHAT' : 'RUNNING';
            status.title = phase === 'unknown' ? '正在确认连接'
              : phase === 'logout' ? '等待登录微信' : '机器人可用';
            status.message = phase === 'logout' ? '请在微信窗口完成登录。' : '';
            status.runtime = {...status.runtime, connection_ready: true,
              account_status_available: phase !== 'unknown',
              hook_ready: phase !== 'unknown',
              wechat_identity: phase === 'recovered' ? 'wxid_current' : ''};
            return result;
          };
        }""")
        expect(page.locator("#stateTitle")).to_have_text("正在确认连接")
        expect(page.locator("#sideHealthText")).to_have_text("正在确认连接")
        expect(page.locator("#signalMessage")).to_have_text("正在确认连接")
        expect(page.locator("#accountText")).to_have_text("正在确认微信连接…")
        expect(page.locator("#stateMessage")).not_to_contain_text("登录")
        page.screenshot(path=str(tmp_path / "download-connection-check.png"))

        page.evaluate("window.testDownloadPhase = 'recovered'")
        expect(page.locator("#stateTitle")).to_have_text("机器人可用")
        expect(page.locator("#signalMessage")).to_have_text("通信正常")
        page.evaluate("window.testDownloadPhase = 'logout'")
        expect(page.locator("#stateTitle")).to_have_text("等待登录微信")
        expect(page.locator("#stateMessage")).to_have_text("请在微信窗口完成登录。")
        assert page.url == manager_url
        assert page.title()
        assert not errors, errors
    finally:
        page.close()


@pytest.mark.parametrize("target,method", [("groups", "get_groups"), ("schedules", "get_scheduled_task_context")])
def test_first_page_read_resumes_when_the_same_account_recovers(browser, manager_url, target, method):
    page = browser.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        page.goto(manager_url)
        expect(page.locator("#stateTitle")).to_have_text("机器人可用")
        if page.locator("#onboardingRecommended").is_visible():
            page.locator("#onboardingRecommended").click()
        page.evaluate("""method => {
          const api = window.pywebview.api, status = api.get_status, read = api[method];
          window.accountProbeAvailable = false;
          window.pageReadCount = 0;
          api.get_status = async () => {
            const result = structuredClone(await status());
            result.data.runtime.account_status_available = window.accountProbeAvailable;
            return result;
          };
          api[method] = (...args) => { window.pageReadCount++; return read(...args); };
        }""", method)
        expect(page.locator("#signalWechatDetail")).to_contain_text("状态待确认")
        page.locator(f'.nav-item[data-page="{target}"]').click()
        expect(page.locator(f"#page-{target}")).to_be_visible()
        reads = page.evaluate("window.pageReadCount")
        page.evaluate("window.accountProbeAvailable = true")
        expect(page.locator("#signalWechatDetail")).to_contain_text("已连接")
        assert page.evaluate("window.pageReadCount") > reads
        if target == "groups":
            expect(page.locator("#groupRows .group-row").first).to_be_visible()
        else:
            expect(page.locator("#scheduleContextMessage")).to_be_hidden()
        assert not errors, errors
    finally:
        page.close()


@pytest.mark.parametrize("missing_script", ["subscription.js", "core-settings.js", "chat-profiles.js", "panel-layout.js", "features.js", "scheduled-tasks.js", "preview.js"])
def test_manager_reports_missing_startup_dependency(browser, manager_url, missing_script, tmp_path: Path):
    page = browser.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.route(f"**/{missing_script}*", lambda route: route.fulfill(status=404, body=""))
    try:
        page.goto(manager_url)
        expect(page.locator("#stateTitle")).to_have_text("软件未能完成初始化")
        expect(page.locator("#stateMessage")).to_contain_text("重新打开软件")
        expect(page.locator("#globalStatus")).to_have_text("初始化失败")
        expect(page.locator("#sideHealthText")).to_have_text("初始化失败")
        expect(page.locator("#primaryAction")).to_be_disabled()
        assert not errors, errors
        page.screenshot(path=str(tmp_path / "startup-error.png"))
    finally:
        page.close()


def test_browser_failure_evidence_survives_early_assertion(browser, manager_url, tmp_path):
    page = browser.new_page()
    page.route("**/settings.js*", lambda route: route.abort("failed"))
    with pytest.raises(AssertionError, match="synthetic early assertion"):
        try:
            page.goto(manager_url)
            expect(page.locator("#stateTitle")).to_have_text("软件未能完成初始化")
            page.evaluate("""() => {
              console.error('synthetic browser diagnostic');
              setTimeout(() => { window.evidenceErrorRaised = true; throw new Error('synthetic page error'); }, 0);
            }""")
            page.wait_for_function("window.evidenceErrorRaised === true")
            raise AssertionError("synthetic early assertion")
        finally:
            page.close()

    evidence_root = tmp_path / "browser-failure/1"
    evidence = json.loads((evidence_root / "browser.json").read_text(encoding="utf-8"))
    assert (evidence_root / "page.png").is_file()
    assert evidence["url"] == manager_url
    assert evidence["state"]["text"]["stateTitle"] == "软件未能完成初始化"
    assert any(item["kind"] == "requestfailed" and "/settings.js" in item["url"] for item in evidence["events"])
    assert any(item["kind"] == "console" and item["text"] == "synthetic browser diagnostic" for item in evidence["events"])
    assert any(item["kind"] == "pageerror" and "synthetic page error" in item["text"] for item in evidence["events"])
    assert any("资源缺失" in item["message"] for item in evidence["state"]["reportedErrors"])


@pytest.mark.parametrize("failure", ["directory", "write", "report"])
def test_browser_evidence_failure_keeps_original_assertion_and_closes_page(tmp_path, monkeypatch, failure):
    from conftest import manager_browser_failure_evidence

    calls = []

    class FakePage:
        url = "http://127.0.0.1:1/?preview=1"
        closed = False

        def add_init_script(self, _source): pass
        def on(self, _event, _handler): pass
        def is_closed(self): return self.closed
        def evaluate(self, _source): return {"synthetic": True}
        def screenshot(self, **_kwargs): pass

        def close(self):
            calls.append("close")
            self.closed = True
            raise RuntimeError("synthetic page close failed")

    page = FakePage()
    browser = SimpleNamespace(new_page=lambda: page)

    def report(*args):
        calls.append("report")
        if failure == "report":
            raise RuntimeError("synthetic report failed")

    request = SimpleNamespace(
        fixturenames=["browser", "manager_url"],
        getfixturevalue=lambda name: browser if name == "browser" else tmp_path,
        node=SimpleNamespace(add_report_section=report),
    )
    manager_browser_failure_evidence.__wrapped__(request, monkeypatch)
    observed = browser.new_page()
    denied = lambda *_args, **_kwargs: (_ for _ in ()).throw(PermissionError("synthetic diagnostic write denied"))
    if failure == "directory":
        monkeypatch.setattr(Path, "mkdir", denied)
    elif failure == "write":
        monkeypatch.setattr(Path, "write_text", denied)
    original = AssertionError("original early assertion")
    with pytest.raises(AssertionError) as caught:
        try:
            raise original
        finally:
            observed.close()
    assert caught.value is original
    assert observed.closed
    assert calls.count("close") == 1
    assert "report" in calls
    with pytest.raises(RuntimeError, match="synthetic page close failed"):
        observed.close()


@pytest.mark.parametrize("outcome", ["running", "stop", "failure"])
def test_start_remains_single_and_explains_login_continuation(browser, manager_url, tmp_path, outcome):
    page = browser.new_page(viewport={"width": 1380, "height": 860})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        page.goto(manager_url)
        expect(page.locator("#stateTitle")).to_have_text("机器人可用")
        if page.locator("#onboardingRecommended").is_visible():
            page.locator("#onboardingRecommended").click()
        page.evaluate("""async () => {
          const api = window.pywebview.api;
          const base = structuredClone((await api.get_status()).data);
          window.startRequests = 0; window.stopRequests = 0; window.startStatusReads = 0;
          window.startPhase = 'idle';
          api.get_status = async () => {
            document.body.dataset.startStatusReads = String(++window.startStatusReads);
            const status = structuredClone(base), phase = window.startPhase;
            Object.assign(status, {start_pending: ['login', 'loading'].includes(phase),
              busy: phase === 'loading', online: phase === 'running',
              can_start: ['idle', 'failure'].includes(phase), can_stop: phase !== 'loading',
              state: {idle:'LICENSE_READY', login:'WAITING_WECHAT', loading:'CORE_STARTING',
                running:'RUNNING', failure:'ERROR'}[phase],
              title: {idle:'机器人未启动', login:'等待登录微信', loading:'微信已登录，正在启动机器人功能',
                running:'机器人可用', failure:'机器人启动失败'}[phase],
              message: phase === 'login' ? '请在微信窗口完成登录；登录后会自动继续启动，无需再次点击启动。'
                : phase === 'loading' ? '账号连接成功，剩余功能正在后台加载。' : '',
              error: phase === 'failure' ? {code:'13001', title:'机器人启动失败', message:'模拟启动失败', solutions:[]} : null});
            Object.assign(status.runtime, {account_status_available:true, connection_ready:true, hook_ready:true,
              wechat_identity: phase === 'login' ? '' : 'wxid_preview_start',
              engine_ready: phase === 'running', upstream_connected:phase === 'running', heartbeat_fresh:phase === 'running'});
            return {ok:true, data:status};
          };
          api.start_robot = () => {
            window.startRequests++;
            return new Promise(resolve => { window.finishStartRequest = () => {
              window.startPhase = 'login'; resolve({ok:true, data:{accepted:true}});
            }; });
          };
          api.stop_robot = async () => {
            window.stopRequests++; window.startPhase = 'idle';
            return {ok:true, data:{accepted:true}};
          };
        }""")
        expect(page.locator("#stateTitle")).to_have_text("机器人未启动")
        page.locator("#primaryAction").click()
        page.wait_for_function("() => window.startRequests === 1")
        expect(page.locator("#primaryAction")).to_be_disabled()
        page.locator("#primaryAction").dispatch_event("click")
        assert page.evaluate("window.startRequests") == 1
        reads_before_next_poll = page.evaluate("window.startStatusReads")
        expect(page.locator("body")).not_to_have_attribute("data-start-status-reads", str(reads_before_next_poll))
        expect(page.locator("#stateTitle")).to_have_text("微信已登录，正在确认启动")
        expect(page.locator("#primaryAction")).to_be_disabled()
        page.evaluate("window.finishStartRequest()")
        expect(page.locator("#stateTitle")).to_have_text("等待登录微信")
        expect(page.locator("#stateMessage")).to_contain_text("自动继续启动")
        expect(page.locator("#primaryAction")).to_have_text("取消启动")
        expect(page.locator("#primaryAction")).to_be_enabled()

        if outcome == "stop":
            page.locator("#primaryAction").click()
            expect(page.locator("#stateTitle")).to_have_text("机器人未启动")
            expect(page.locator("#primaryAction")).to_have_text("启动")
            expect(page.locator("#primaryAction")).to_be_enabled()
            assert page.evaluate("window.stopRequests") == 1
        else:
            page.evaluate("window.startPhase = 'loading'")
            expect(page.locator("#stateTitle")).to_have_text("微信已登录，正在启动机器人功能")
            expect(page.locator("#globalStatus")).to_have_text("正在启动")
            expect(page.locator("#stageCore em")).to_have_text("启动中")
            expect(page.locator("#primaryAction")).to_have_text("启动中…")
            expect(page.locator("#primaryAction")).to_be_disabled()
            page.locator("#primaryAction").dispatch_event("click")
            assert page.evaluate("window.startRequests") == 1
            page.screenshot(path=str(tmp_path / "login-continues-without-second-start.png"))
            page.evaluate("phase => { window.startPhase = phase; }", "running" if outcome == "running" else "failure")
            expect(page.locator("#stateTitle")).to_have_text("机器人可用" if outcome == "running" else "机器人启动失败")
            expect(page.locator("#primaryAction")).to_be_enabled()
            expect(page.locator("#primaryAction")).to_have_text("停止运行" if outcome == "running" else "启动")
        assert page.evaluate("window.startRequests") == 1
        assert not errors, errors
        assert page.url == manager_url and "管理" in page.title()
        expect(page.locator("#page-home")).to_be_visible()
    finally:
        page.close()


@pytest.mark.parametrize("outcome", ["cancel", "connect_failure", "start_rejected"])
def test_start_preparation_restores_raw_status_when_followup_reads_fail(
    browser, manager_url, tmp_path, outcome,
):
    page = browser.new_page(viewport={"width": 1380, "height": 860})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        page.goto(manager_url + ("&license=pending" if outcome != "start_rejected" else ""))
        expect(page.locator("#versionText")).not_to_have_text("v0.0.0")
        if page.locator("#onboardingRecommended").is_visible():
            page.locator("#onboardingRecommended").click()
        page.evaluate("""async () => {
          const api = window.pywebview.api, idle = (await api.get_status()).data;
          Object.assign(idle, {state:'LICENSE_READY', title:'机器人未启动', message:'可以启动机器人。',
            busy:false, online:false, can_start:true, can_stop:false, start_pending:false, error:null});
          Object.assign(idle.runtime, {wechat_identity:'', account_status_available:true,
            connection_ready:false, engine_ready:false, hook_ready:false});
          idle.environment.ready = true;
          window.preparationStarts = 0; window.failPreparationStatus = false;
          api.get_status = async () => {
            if (window.failPreparationStatus) {
              document.body.dataset.preparationReadFailed = 'true';
              return {ok:false, error:{code:'15001', message:'模拟状态读取失败'}};
            }
            return {ok:true, data:structuredClone(idle)};
          };
          api.check_environment = async () => ({ok:true, data:structuredClone(idle)});
          api.connect_license_wechat = async () => ({ok:false, error:{message:'模拟连接失败'}});
          api.start_robot = async () => {
            window.preparationStarts++; window.failPreparationStatus = true;
            return {ok:true, data:{accepted:false}};
          };
        }""")
        page.locator('.nav-item[data-page="home"]').click()
        expect(page.locator("#stateTitle")).to_have_text("机器人未启动")
        page.locator("#primaryAction").click()
        if outcome != "start_rejected":
            expect(page.locator("#modalTitle")).to_have_text("确认绑定当前套餐")
            expect(page.locator("#primaryAction")).to_be_disabled()
            page.locator("#primaryAction").dispatch_event("click")
            assert page.evaluate("window.preparationStarts") == 0
            # A real status poll must render the preparation projection before it is cancelled.
            expect(page.locator("#stateTitle")).to_have_text("正在准备启动机器人")
            page.evaluate("window.failPreparationStatus = true")
            page.locator("#modalCancel" if outcome == "cancel" else "#modalConfirm").click()
            expect(page.locator("#modalBackdrop")).to_be_hidden()
            if outcome == "connect_failure":
                expect(page.locator("#toastRegion")).to_contain_text("模拟连接失败")
        expect(page.locator("#stateTitle")).to_have_text("机器人未启动")
        expect(page.locator("#primaryAction")).to_be_enabled()
        expect(page.locator("#primaryAction")).to_have_attribute("aria-busy", "false")
        expect(page.locator("body")).to_have_attribute("data-preparation-read-failed", "true")
        assert page.evaluate("window.preparationStarts") == (1 if outcome == "start_rejected" else 0)
        page.screenshot(path=str(tmp_path / f"preparation-{outcome}-raw-status-restored.png"))
        if outcome != "start_rejected":
            page.locator("#primaryAction").click()
            expect(page.locator("#modalTitle")).to_have_text("确认绑定当前套餐")
            page.locator("#modalCancel").click()
            expect(page.locator("#primaryAction")).to_be_enabled()
            assert page.evaluate("window.preparationStarts") == 0
        assert not errors, errors
    finally:
        page.close()


@pytest.mark.parametrize("response", ["accepted", "rejected", "failure"])
def test_slow_start_can_cancel_once_and_recovers_from_rejected_stop(browser, manager_url, tmp_path, response):
    page = browser.new_page(viewport={"width": 1380, "height": 860})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        page.goto(manager_url)
        expect(page.locator("#stateTitle")).to_have_text("机器人可用")
        if page.locator("#onboardingRecommended").is_visible():
            page.locator("#onboardingRecommended").click()
        page.evaluate("""async () => {
          const api = window.pywebview.api, base = structuredClone((await api.get_status()).data);
          window.cancelPhase = 'idle'; window.cancelStarts = 0; window.cancelStops = 0; window.cancelReads = 0;
          api.get_status = async () => {
            document.body.dataset.cancelReads = String(++window.cancelReads);
            const status = structuredClone(base), phase = window.cancelPhase;
            Object.assign(status, {state: {idle:'LICENSE_READY', loading:'CORE_STARTING', stopping:'STOPPING'}[phase],
              title: {idle:'机器人未启动', loading:'正在启动机器人', stopping:'正在取消启动'}[phase],
              message:'', start_pending:phase === 'loading', busy:phase !== 'idle', online:false,
              can_start:phase === 'idle', can_stop:phase === 'loading', error:null});
            Object.assign(status.runtime, {engine_ready:false, connection_ready:true,
              account_status_available:true, wechat_identity:'wxid_demo_account'});
            return {ok:true, data:status};
          };
          api.start_robot = async () => {
            window.cancelStarts++; window.cancelPhase = 'loading';
            return {ok:true, data:{accepted:true}};
          };
          api.stop_robot = () => {
            window.cancelStops++;
            return new Promise(resolve => { window.finishCancel = response => resolve(response === 'failure'
              ? {ok:false, error:{message:'模拟取消失败'}} : {ok:true, data:{accepted:response !== 'rejected'}}); });
          };
        }""")
        expect(page.locator("#stateTitle")).to_have_text("机器人未启动")
        page.locator("#primaryAction").click()
        expect(page.locator("#primaryAction")).to_have_text("取消启动")
        expect(page.locator("#primaryAction")).to_be_enabled()
        expect(page.locator("#primaryAction")).to_have_attribute("aria-busy", "true")
        page.locator("#primaryAction").click()
        expect(page.locator("#primaryAction")).to_have_text("正在停止…")
        expect(page.locator("#primaryAction")).to_be_disabled()
        reads = page.evaluate("window.cancelReads")
        expect(page.locator("body")).not_to_have_attribute("data-cancel-reads", str(reads))
        expect(page.locator("#primaryAction")).to_be_disabled()
        page.locator("#primaryAction").dispatch_event("click")
        assert page.evaluate("window.cancelStops") == 1
        page.evaluate("response => window.finishCancel(response)", response)
        if response != "accepted":
            expect(page.locator("#primaryAction")).to_have_text("取消启动")
            expect(page.locator("#primaryAction")).to_be_enabled()
            expect(page.locator("#toastRegion")).to_contain_text("模拟取消失败" if response == "failure" else "停止请求未被接受")
            page.locator("#primaryAction").click()
            assert page.evaluate("window.cancelStops") == 2
            page.evaluate("window.finishCancel('accepted')")
        reads = page.evaluate("window.cancelReads")
        expect(page.locator("body")).not_to_have_attribute("data-cancel-reads", str(reads))
        expect(page.locator("#primaryAction")).to_be_disabled()
        expect(page.locator("#primaryAction")).to_have_text("正在停止…")
        page.locator("#primaryAction").dispatch_event("click")
        assert page.evaluate("window.cancelStops") == (1 if response == "accepted" else 2)
        page.evaluate("window.cancelPhase = 'stopping'")
        expect(page.locator("#stateTitle")).to_have_text("正在取消启动")
        expect(page.locator("#globalStatus")).to_have_text("正在停止")
        expect(page.locator("#primaryAction")).to_have_text("正在停止…")
        expect(page.locator("#primaryAction")).to_be_disabled()
        page.evaluate("window.cancelPhase = 'idle'")
        expect(page.locator("#primaryAction")).to_have_text("启动")
        expect(page.locator("#primaryAction")).to_be_enabled()
        assert page.evaluate("window.cancelStarts") == 1
        assert not errors, errors
        page.screenshot(path=str(tmp_path / f"slow-start-cancel-{response}.png"))
    finally:
        page.close()


@pytest.mark.parametrize("method, control", [
    ("check_environment", "#settingsCheckWechat"),
    ("select_wechat", "#settingsChooseWechat"),
])
def test_late_environment_response_cannot_restore_cancelled_start(browser, manager_url, tmp_path, method, control):
    page = browser.new_page(viewport={"width": 1380, "height": 860})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        page.goto(manager_url)
        expect(page.locator("#stateTitle")).to_have_text("机器人可用")
        if page.locator("#onboardingRecommended").is_visible():
            page.locator("#onboardingRecommended").click()
        page.evaluate("""async method => {
          const api = window.pywebview.api, base = structuredClone((await api.get_status()).data);
          window.environmentPhase = 'idle'; window.environmentStarts = 0; window.environmentStops = 0;
          const snapshot = () => {
            const status = structuredClone(base), phase = window.environmentPhase;
            Object.assign(status, {state:{idle:'LICENSE_READY', loading:'CORE_STARTING', stopping:'STOPPING'}[phase],
              title:{idle:'机器人未启动', loading:'正在启动机器人', stopping:'正在取消启动'}[phase],
              message:'', start_pending:phase === 'loading', busy:phase !== 'idle', online:false,
              can_start:phase === 'idle', can_stop:phase === 'loading', error:null});
            Object.assign(status.runtime, {engine_ready:false, connection_ready:true,
              account_status_available:true, wechat_identity:'wxid_demo_account'});
            return {ok:true, data:status};
          };
          api.get_status = async () => snapshot();
          api.start_robot = async () => {
            window.environmentStarts++; window.environmentPhase = 'loading';
            return {ok:true, data:{accepted:true}};
          };
          api.stop_robot = () => {
            window.environmentStops++; window.environmentPhase = 'stopping';
            return new Promise(resolve => { window.finishEnvironmentStop = () => resolve({ok:true, data:{accepted:true}}); });
          };
          api[method] = () => {
            const stale = snapshot(); document.body.dataset.environmentHeld = 'true';
            return new Promise(resolve => { window.releaseEnvironment = () => {
              resolve(stale);
              window.setTimeout(() => { document.body.dataset.environmentReleased = 'true'; }, 0);
            }; });
          };
        }""", method)
        expect(page.locator("#stateTitle")).to_have_text("机器人未启动")
        page.locator("#primaryAction").click()
        expect(page.locator("#primaryAction")).to_have_text("取消启动")
        expect(page.locator("#primaryAction")).to_be_enabled()
        page.locator('.nav-item[data-page="settings"]').click()
        page.locator('[data-settings-tab="system"]').click()
        page.locator(control).click()
        expect(page.locator("body")).to_have_attribute("data-environment-held", "true")
        page.locator('.nav-item[data-page="home"]').click()
        page.locator("#primaryAction").click()
        expect(page.locator("#stateTitle")).to_have_text("正在取消启动")
        expect(page.locator("#primaryAction")).to_be_disabled()
        page.evaluate("""() => {
          window.titlesAfterEnvironmentRelease = [];
          new MutationObserver(() => window.titlesAfterEnvironmentRelease.push(document.querySelector('#stateTitle').textContent))
            .observe(document.querySelector('#stateTitle'), {childList:true});
          window.releaseEnvironment();
        }""")
        expect(page.locator("body")).to_have_attribute("data-environment-released", "true")
        expect(page.locator("#stateTitle")).to_have_text("正在取消启动")
        expect(page.locator("#primaryAction")).to_have_text("正在停止…")
        expect(page.locator("#primaryAction")).to_be_disabled()
        page.locator("#primaryAction").dispatch_event("click")
        page.evaluate("window.environmentPhase = 'idle'")
        expect(page.locator("#stateTitle")).to_have_text("机器人未启动")
        expect(page.locator("#primaryAction")).to_have_text("正在停止…")
        expect(page.locator("#primaryAction")).to_be_disabled()
        page.locator("#primaryAction").dispatch_event("click")
        assert page.evaluate("window.environmentStarts") == 1
        assert page.evaluate("window.environmentStops") == 1
        assert "正在启动机器人" not in page.evaluate("window.titlesAfterEnvironmentRelease")
        page.evaluate("window.finishEnvironmentStop()")
        expect(page.locator("#primaryAction")).to_have_text("启动")
        expect(page.locator("#primaryAction")).to_be_enabled()
        assert not errors, errors
        page.screenshot(path=str(tmp_path / f"late-{method}-preserves-stop.png"))
    finally:
        page.close()


def test_late_idle_poll_cannot_undo_an_accepted_start(browser, manager_url, tmp_path):
    page = browser.new_page(viewport={"width": 1380, "height": 860})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        page.goto(manager_url)
        expect(page.locator("#stateTitle")).to_have_text("机器人可用")
        if page.locator("#onboardingRecommended").is_visible():
            page.locator("#onboardingRecommended").click()
        page.evaluate("""async () => {
          const api = window.pywebview.api, idle = (await api.get_status()).data;
          Object.assign(idle, {state:'LICENSE_READY', title:'机器人未启动', message:'可以启动机器人。',
            busy:false, online:false, can_start:true, can_stop:false, start_pending:false, error:null});
          Object.assign(idle.runtime, {engine_ready:false, connection_ready:true,
            account_status_available:true, wechat_identity:'wxid_demo_account'});
          window.delayIdlePoll = false; window.acceptedStarts = 0; window.acceptedStart = false;
          api.get_status = () => {
            if (window.delayIdlePoll) {
              window.delayIdlePoll = false;
              document.body.dataset.idlePollHeld = 'true';
              return new Promise(resolve => { window.releaseIdlePoll = () => resolve({ok:true, data:structuredClone(idle)}); });
            }
            const status = structuredClone(idle);
            if (window.acceptedStart) Object.assign(status, {state:'CORE_STARTING',
              title:'微信已登录，正在启动机器人功能', busy:true, can_start:false, start_pending:true});
            return Promise.resolve({ok:true, data:status});
          };
          api.start_robot = async () => {
            window.acceptedStarts++; window.acceptedStart = true;
            document.body.dataset.startAccepted = 'true';
            return {ok:true, data:{accepted:true}};
          };
          window.titlesAfterStart = [];
          new MutationObserver(() => {
            if (window.acceptedStart) window.titlesAfterStart.push(document.querySelector('#stateTitle').textContent);
          }).observe(document.querySelector('#stateTitle'), {childList:true});
        }""")
        expect(page.locator("#stateTitle")).to_have_text("机器人未启动")
        page.evaluate("window.delayIdlePoll = true")
        expect(page.locator("body")).to_have_attribute("data-idle-poll-held", "true")
        page.locator("#primaryAction").click()
        expect(page.locator("body")).to_have_attribute("data-start-accepted", "true")
        expect(page.locator("#primaryAction")).to_be_disabled()
        page.evaluate("window.releaseIdlePoll()")
        expect(page.locator("#stateTitle")).to_have_text("微信已登录，正在启动机器人功能")
        expect(page.locator("#primaryAction")).to_be_disabled()
        expect(page.locator("#primaryAction")).to_have_attribute("aria-busy", "true")
        assert "机器人未启动" not in page.evaluate("window.titlesAfterStart")
        assert page.evaluate("window.acceptedStarts") == 1
        assert not errors, errors
        page.screenshot(path=str(tmp_path / "late-idle-poll-preserves-accepted-start.png"))
    finally:
        page.close()
