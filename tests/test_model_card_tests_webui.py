"""Model card tests use the delivered UI and synthetic, controllable probes."""
import re

import pytest
from playwright.sync_api import expect

from test_manager_startup import browser, manager_url  # noqa: F401
from test_model_layout_webui import mock_model_cards


@pytest.fixture
def cards_page(browser, manager_url):
    page = browser.new_page(viewport={"width": 1440, "height": 1024}, reduced_motion="reduce")
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    mock_model_cards(page)
    page.goto(manager_url)
    expect(page).to_have_title(re.compile("机器人管理$"))
    expect(page.locator("#versionText")).not_to_have_text("v0.0.0")
    if page.locator("#onboardingRecommended").is_visible():
        page.locator("#onboardingRecommended").click()
    page.locator('.nav-item[data-page="settings"]').click()
    page.evaluate("""async () => {
      const api = window.pywebview.api;
      window.cardServices = structuredClone((await api.get_providers()).data).map(item => ({
        ...item, api_base: item.api_base || 'https://example.test/v1', last_test: null,
        is_default_embedding: item.id === 'custom/2', configured: item.id !== 'custom/5',
        service_kind: item.id === 'custom/6' ? 'embedding' : item.service_kind,
      }));
      window.cardProbeCalls = [];
      window.cardProbePending = new Map();
      window.cardProbeActive = 0;
      window.cardProbeMaxActive = 0;
      window.embeddingSwitchCalls = [];
      const identity = item => JSON.stringify([item.model, item.api_base, item.service_kind, item.thinking_enabled]);
      api.get_providers = () => ({ok:true, data:structuredClone(window.cardServices)});
      api.set_provider_thinking = (id, enabled) => {
        const service = window.cardServices.find(item => item.id === id);
        service.thinking_enabled = enabled;
        service.last_test = null;
        return {ok:true, data:{id, thinking_enabled:enabled, apply_state:'applied'}};
      };
      api.set_default_embedding_provider = id => {
        window.embeddingSwitchCalls.push(id);
        window.cardServices.forEach(item => { item.is_default_embedding = item.id === id; });
        return {ok:true, data:{id, apply_state:'applied'}};
      };
      api.delete_provider = id => {
        window.cardServices = window.cardServices.filter(item => item.id !== id);
        return {ok:true, data:{id}};
      };
      api.save_provider = values => {
        const service = window.cardServices.find(item => item.id === values.provider_id);
        Object.assign(service, {model:values.model, api_base:values.api_base,
          service_kind:values.service_kind, thinking_enabled:values.thinking_enabled, last_test:null});
        return {ok:true, data:{...structuredClone(service), apply_state:'applied'}};
      };
      api.test_provider = id => {
        const service = window.cardServices.find(item => item.id === id);
        window.cardProbeCalls.push(id);
        window.cardProbeActive += 1;
        window.cardProbeMaxActive = Math.max(window.cardProbeMaxActive, window.cardProbeActive);
        return new Promise(resolve => window.cardProbePending.set(id, {resolve, identity:identity(service)}));
      };
      window.finishCardProbe = (id, failed = false, discard = false) => {
        const pending = window.cardProbePending.get(id);
        if (!pending) throw new Error('No pending synthetic probe for ' + id);
        const service = window.cardServices.find(item => item.id === id);
        const last_test = discard || !service || identity(service) !== pending.identity ? null : {
          status:failed ? 'error' : 'success', elapsed_ms:failed ? 2450 : 1250,
          tested_at:'2026-11-20T01:00:00+00:00', error_code:failed ? '16007' : '',
        };
        if (last_test) service.last_test = last_test;
        window.cardProbeActive -= 1;
        window.cardProbePending.delete(id);
        pending.resolve(failed ? {ok:false, error:{code:'16007', title:'模拟模型超时',
          message:'本次测试请求超时', detail:'本次测试请求超时', recorded:true, last_test}}
          : {ok:true, data:{id, model:service?.model || 'removed', status:'available',
            test_kind:service?.service_kind || 'chat', response_text:'PONG synthetic', elapsed_ms:1250, last_test}});
      };
    }""")
    # An existing card action fetches the synthetic catalog through get_providers.
    page.locator('[data-provider-thinking="custom/1"]').click()
    expect(card(page, 2).locator(".provider-card-status")).to_have_text("默认向量")
    expect(card(page, 5).locator("[data-provider-test]")).to_be_disabled()
    try:
        yield page
        assert not errors, errors
    finally:
        page.close()


def card(page, index):
    return page.locator(f'[data-provider-card="custom/{index}"]')


def finish_probe(page, index, *, failed=False, discard=False):
    page.evaluate("args => window.finishCardProbe(...args)", [f"custom/{index}", failed, discard])


@pytest.mark.parametrize("failed", [False, True])
def test_card_probe_result_survives_catalog_refresh(cards_page, tmp_path, failed):
    page = cards_page
    target = card(page, 0)
    target.locator("[data-provider-test]").click()
    expect(target.locator(".provider-card-test")).to_have_attribute("data-status", "pending")
    expect(target.locator("[data-provider-test]")).to_be_disabled()
    # Disabled card actions and the editor both deduplicate the existing request.
    target.locator("[data-provider-test]").evaluate("button => button.click()")
    target.locator("footer [data-provider-edit]").click()
    page.locator("#testProvider").click()
    assert page.evaluate("window.cardProbeCalls") == ["custom/0"]
    finish_probe(page, 0, failed=failed)
    page.locator("#closeProviderEditor").click()
    expected_status = "error" if failed else "success"
    expect(target.locator(".provider-card-test")).to_have_attribute("data-status", expected_status)
    expect(target.locator(".provider-card-test")).to_have_text("失败" if failed else "1.3 秒")
    if failed:
        expect(target.locator(".provider-card-test")).to_have_attribute("title", re.compile("2.5 秒"))
        expect(target.locator(".provider-card-test")).to_have_attribute("title", re.compile("16007"))
    page.locator('[data-provider-thinking="custom/1"]').click()
    expect(page.locator("#providerReadbackFeedback")).to_be_hidden()
    expect(target.locator(".provider-card-test")).to_have_attribute("data-status", expected_status)
    expect(target.locator("[data-provider-test]")).to_be_enabled()
    page.screenshot(path=str(tmp_path / f"model-card-test-{expected_status}.png"), animations="disabled")


def test_batch_probe_limits_concurrency_and_continues_after_failure(cards_page):
    page = cards_page
    page.locator("[data-provider-test-all]").click()
    expect(page.locator("[data-provider-test-all]")).to_have_text("停止测试")
    expect(card(page, 0).locator(".provider-card-test")).to_have_attribute("data-status", "pending")
    expect(card(page, 1).locator(".provider-card-test")).to_have_attribute("data-status", "pending")
    assert page.evaluate("window.cardProbeCalls") == ["custom/0", "custom/1"]
    for index, next_index in [(0, 2), (1, 3), (2, 4), (3, 6)]:
        finish_probe(page, index, failed=index == 0)
        expect(card(page, next_index).locator(".provider-card-test")).to_have_attribute("data-status", "pending")
    finish_probe(page, 4)
    finish_probe(page, 6)
    expect(page.locator(".provider-batch-status")).to_have_text("测试完成 6/6 · 成功 5 · 失败 1")
    expect(page.locator("[data-provider-test-all]")).to_have_text("测试所有模型")
    expect(card(page, 5).locator("[data-provider-test]")).to_be_disabled()
    assert page.evaluate("window.cardProbeMaxActive") == 2
    assert page.evaluate("window.cardProbeActive") == 0
    assert page.evaluate("window.cardProbeCalls") == [f"custom/{index}" for index in [0, 1, 2, 3, 4, 6]]


def test_editor_probe_result_is_shared_with_model_card(cards_page):
    page = cards_page
    card(page, 0).locator("footer [data-provider-edit]").click()
    page.locator("#testProvider").click()
    expect(page.locator("#testProvider")).to_be_disabled()
    finish_probe(page, 0)
    expect(page.locator("#providerTestTitle")).to_have_text("本次模型测试成功")
    expect(page.locator("#providerTestOutput")).to_have_text("PONG synthetic")
    page.locator("#closeProviderEditor").click()
    expect(card(page, 0).locator(".provider-card-test")).to_have_attribute("data-status", "success")
    expect(card(page, 0).locator(".provider-card-test")).to_have_text("1.3 秒")
    assert page.evaluate("window.cardProbeCalls") == ["custom/0"]


def test_batch_waits_for_existing_card_probe_before_scheduling(cards_page):
    page = cards_page
    card(page, 4).locator("[data-provider-test]").click()
    expect(card(page, 4).locator(".provider-card-test")).to_have_attribute("data-status", "pending")
    batch_button = page.locator("[data-provider-test-all]")
    if batch_button.is_disabled():
        finish_probe(page, 4)
        expect(batch_button).to_be_enabled()
    else:
        batch_button.click()
        expect(batch_button).to_have_text("停止测试")
        assert page.evaluate("window.cardProbeMaxActive") <= 2
        batch_button.click()
        for provider_id in page.evaluate("Array.from(window.cardProbePending.keys())"):
            page.evaluate("id => window.finishCardProbe(id)", provider_id)
        expect(batch_button).to_have_text("测试所有模型")
        assert page.evaluate("window.cardProbeCalls") == ["custom/4", "custom/0"]
        expect(card(page, 1).locator(".provider-card-test")).to_have_attribute("data-status", "untested")


def test_batch_stop_finishes_pending_requests_without_starting_more(cards_page):
    page = cards_page
    page.locator("[data-provider-test-all]").click()
    expect(card(page, 1).locator(".provider-card-test")).to_have_attribute("data-status", "pending")
    page.locator("[data-provider-test-all]").click()
    expect(page.locator("[data-provider-test-all]")).to_have_text("正在停止…")
    expect(page.locator("[data-provider-test-all]")).to_be_disabled()
    finish_probe(page, 0)
    finish_probe(page, 1, failed=True)
    expect(page.locator(".provider-batch-status")).to_have_text("已停止 2/6 · 成功 1 · 失败 1")
    expect(page.locator("[data-provider-test-all]")).to_be_enabled()
    expect(card(page, 2).locator(".provider-card-test")).to_have_attribute("data-status", "untested")
    assert page.evaluate("window.cardProbeCalls") == ["custom/0", "custom/1"]


@pytest.mark.parametrize("change", ["saved_configuration", "backend_discard"])
def test_stale_probe_cannot_mark_changed_configuration_available(cards_page, change):
    page = cards_page
    target = card(page, 0)
    target.locator("[data-provider-test]").click()
    expect(target.locator(".provider-card-test")).to_have_attribute("data-status", "pending")
    if change == "saved_configuration":
        target.locator("footer [data-provider-edit]").click()
        page.locator("#providerModelInput").fill("changed-model")
        page.locator("#saveProvider").click()
        expect(page.locator("#saveProvider")).to_be_enabled()
        expect(page.locator("#providerEditorStatus")).to_have_text("已保存")
        page.locator("#closeProviderEditor").click()
    finish_probe(page, 0, discard=change == "backend_discard")
    expect(target.locator(".provider-card-test")).to_have_attribute("data-status", "untested")
    expect(target.locator(".provider-card-test")).to_have_text("—")


def test_embedding_default_is_independent_of_reply_default(cards_page):
    page = cards_page
    expect(card(page, 0).locator(".provider-card-status")).to_have_text("使用中")
    expect(card(page, 2).locator(".provider-card-status")).to_have_text("默认向量")
    card(page, 6).locator("[data-provider-embedding-default]").click()
    expect(card(page, 6).locator(".provider-card-status")).to_have_text("默认向量")
    expect(card(page, 2).locator(".provider-card-status")).to_have_text("备用")
    expect(card(page, 0).locator(".provider-card-status")).to_have_text("使用中")
    expect(page.locator(".provider-list-toolbar strong")).to_have_text("mimo-v2.6-pro 正在回复")
    assert page.evaluate("window.embeddingSwitchCalls") == ["custom/6"]
    assert page.evaluate("window.cardServices.filter(item => item.is_default).map(item => item.id)") == ["custom/0"]


@pytest.mark.parametrize("destination", ["page", "section"])
def test_late_card_failure_does_not_notify_another_page(cards_page, destination):
    page = cards_page
    card(page, 0).locator("[data-provider-test]").click()
    expect(card(page, 0).locator(".provider-card-test")).to_have_attribute("data-status", "pending")
    if destination == "page":
        page.locator('.nav-item[data-page="home"]').click()
    else:
        page.locator('[data-settings-tab="skin"]').click()
    finish_probe(page, 0, failed=True)
    expect(card(page, 0).locator("[data-provider-test]")).to_be_enabled()
    expect(page.locator("#toastRegion").get_by_text(re.compile("模拟模型超时"))).to_have_count(0)
    if destination == "page":
        page.locator('.nav-item[data-page="settings"]').click()
    else:
        page.locator('[data-settings-tab="model"]').click()
    expect(card(page, 0).locator(".provider-card-test")).to_have_attribute("data-status", "error")


def test_deleted_model_waiting_for_a_slot_is_not_probed(cards_page):
    page = cards_page
    for index in (0, 1, 3):
        card(page, index).locator("[data-provider-test]").click()
        expect(card(page, index).locator(".provider-card-test")).to_have_attribute("data-status", "pending")
    assert page.evaluate("window.cardProbeCalls") == ["custom/0", "custom/1"]
    card(page, 3).locator("footer [data-provider-delete]").click()
    page.locator("#modalConfirm").click()
    expect(card(page, 3)).to_have_count(0)
    finish_probe(page, 0)
    finish_probe(page, 1)
    expect(card(page, 1).locator("[data-provider-test]")).to_be_enabled()
    assert page.evaluate("window.cardProbeCalls") == ["custom/0", "custom/1"]
    assert page.evaluate("window.cardProbeActive") == 0
