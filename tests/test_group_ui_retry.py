from pathlib import Path
import subprocess

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


def test_group_member_counts_keep_bounded_retries_and_request_deduplication() -> None:
    script = _script()

    assert "const GROUP_MEMBER_RETRY_DELAYS_MS = [800, 2000];" in script
    assert "const groupMemberLoadsInFlight = new Set();" in script
    assert "queueGroupMemberRequest(groupId, generation, background)" in script
    assert "if (groupMemberLoadsInFlight.has(requestKey)) return;" in script
    assert "groupMemberLoadsInFlight.delete(requestKey);" in script
    assert "attempt <= GROUP_MEMBER_RETRY_DELAYS_MS.length" in script
    assert 'background && (state.activePage !== "groups"' in script


def test_group_member_queue_limits_concurrency_and_discards_stale_work() -> None:
    script_path = Path(app_shell.__file__).with_name("app_webui") / "app.js"
    harness = Path(__file__).parent / "js" / "group_member_queue.cjs"
    subprocess.run(["node", str(harness), str(script_path)], check=True, timeout=15)


def test_leaving_group_management_cancels_pending_retries() -> None:
    script = _script()

    assert 'const leavingGroupManagement = state.activePage === "groups" && page !== "groups";' in script
    assert "window.clearTimeout(state.groupRefreshRetryTimer);" in script
    assert "state.groupMemberLoad += 1;" in script
