from pathlib import Path
import subprocess

import pytest


WEBUI = Path(__file__).parents[1] / "src" / "wechatrebot_manager" / "app_webui"


def test_schedule_list_keeps_a_concise_heading_and_integrated_toolbar() -> None:
    markup = (WEBUI / "index.html").read_text(encoding="utf-8")
    schedule = markup[markup.index('id="page-schedules"'):markup.index('id="scheduledTaskEditor"')]

    assert 'id="scheduleTopActions" hidden' in markup
    assert 'id="pageContextLabel"' not in markup
    assert "北京时间 UTC+8" in markup
    assert schedule.count('id="scheduleHeading"') == 1
    assert 'id="scheduleHeadingDescription"' not in schedule
    assert 'class="schedule-stat-pills"' in schedule
    assert 'class="schedule-table-topbar"' in schedule
    assert 'id="scheduleTableTotal"' in schedule
    assert schedule.index('id="scheduledActionFilter"') < schedule.index('id="scheduledTaskRows"')


def test_schedule_empty_state_has_scenario_guidance_and_a_real_create_action() -> None:
    script = (WEBUI / "app.js").read_text(encoding="utf-8")
    styles = (WEBUI / "app.css").read_text(encoding="utf-8")

    assert '$("#scheduleTableTotal").textContent = total;' in script
    assert '$("#scheduledTaskHead").hidden = items.length === 0;' in script
    assert '$("#scheduledTaskTable").classList.toggle("is-empty", items.length === 0);' in script
    assert '"暂无定时任务"' in script
    assert "每日天气预报、定时资讯或一句提醒" in script
    assert "+ 创建第一个定时任务" in script
    assert 'class="schedule-empty-icon"' in script
    assert ".schedule-stat-pills article" in styles
    assert "height: 48px" in styles
    assert ".schedule-empty-icon svg { width: 36px; height: 36px;" in styles
    assert ".schedule-table-scroll table.is-empty { width: 100%; min-width: 0; }" in styles


@pytest.mark.parametrize("scenario", [
    "failed_context_keeps_draft", "unreadable_migration_blocks_legacy",
    "cross_account_rows", "offline_recovery_completion", "overlapping_refresh",
    "duplicate_preview", "stale_preview", "preview_error", "malformed_response",
    "product_error_reason",
])
def test_legacy_migration_ui_state_regressions(scenario: str) -> None:
    harness = Path(__file__).parent / "js" / "legacy_migration_ui.cjs"
    result = subprocess.run(
        ["node", str(harness), str(WEBUI / "app.js"), scenario],
        capture_output=True, text=True, encoding="utf-8", timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
