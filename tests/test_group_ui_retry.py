from pathlib import Path

from wechatrebot_manager import app_shell


def _script() -> str:
    return (
        Path(app_shell.__file__).with_name("app_webui") / "app.js"
    ).read_text(encoding="utf-8")


def test_group_directory_first_load_retries_without_manual_refresh() -> None:
    script = _script()

    assert "const GROUP_DIRECTORY_RETRY_DELAYS_MS = [1500, 4000];" in script
    assert "function scheduleGroupRefreshRetry(retryAttempt)" in script
    assert "void loadGroups(true, {background: true, retryAttempt: retryAttempt + 1});" in script
    assert "if (state.groupStale) scheduleGroupRefreshRetry(retryAttempt);" in script


def test_group_member_counts_retry_with_lower_concurrency() -> None:
    script = _script()

    assert "const GROUP_MEMBER_RETRY_DELAYS_MS = [800, 2000];" in script
    assert "Math.min(2, pending.length)" in script
    assert "attempt <= GROUP_MEMBER_RETRY_DELAYS_MS.length" in script
    assert 'background && (state.activePage !== "groups"' in script


def test_leaving_group_management_cancels_pending_retries() -> None:
    script = _script()

    assert 'const leavingGroupManagement = state.activePage === "groups" && page !== "groups";' in script
    assert "window.clearTimeout(state.groupRefreshRetryTimer);" in script
    assert "state.groupMemberLoad += 1;" in script
