import json
from types import SimpleNamespace

from wechatrebot_manager import portrait_cache
from wechatrebot_manager.app_shell import ApplicationApi


def test_portraits_survive_restart_failure_and_refresh_on_membership_change(tmp_path, monkeypatch):
    jobs = []
    monkeypatch.setattr(portrait_cache, "_POOL", SimpleNamespace(submit=lambda fn, *args: jobs.append((fn, args))))
    monkeypatch.setattr(portrait_cache, "_download", lambda url: "data:image/png;base64," + url.rsplit("/", 1)[-1])
    cache = portrait_cache.PortraitCache(tmp_path)
    item = {"group_id": "room@chatroom", "avatar": "", "member_count": 5}
    fresh = {"avatar": "https://wx.qlogo.cn/AAAA"}
    cache.enrich("bot", item, "room@chatroom", lambda: fresh)
    fn, args = jobs.pop(); fn(*args)
    cache = portrait_cache.PortraitCache(tmp_path)
    assert cache.enrich("bot", item, "room@chatroom")["avatar"] == "data:image/png;base64,AAAA"
    assert not jobs
    fresh["avatar"] = "https://wx.qlogo.cn/BBBB"
    item["member_count"] = 4
    cache.enrich("bot", item, "room@chatroom", lambda: fresh)
    fn, args = jobs.pop(); fn(*args)
    assert cache.enrich("bot", item, "room@chatroom")["avatar"] == "data:image/png;base64,BBBB"
    def unavailable():
        raise OSError("offline")
    cache.enrich("bot", item, "room@chatroom", unavailable, force=True)
    fn, args = jobs.pop(); fn(*args)
    assert cache.read("bot", "room@chatroom")[1] == "data:image/png;base64,BBBB"
    assert cache.read("another-account", "room@chatroom")[0] == {}


def test_avatar_download_does_not_fetch_untrusted_hosts():
    for url in ("http://127.0.0.1/private", "file:///C:/secret", "https://wx.qlogo.cn.evil.test/x"):
        assert portrait_cache._download(url) == ""


def test_assistant_avatar_never_reuses_peer_avatar(tmp_path):
    api = ApplicationApi.__new__(ApplicationApi)
    api._install_root = tmp_path
    api._lifecycle = SimpleNamespace(refresh=lambda: SimpleNamespace(runtime=SimpleNamespace(wechat_identity="wxid_bot")))
    api._history = SimpleNamespace(messages=lambda *args, **kw: [{"role": "assistant", "avatar": "peer.jpg", "sender_id": "wxid_peer"}])
    api._contacts = SimpleNamespace(self_profile=lambda: {"avatar": "robot.jpg"})
    (tmp_path / "config").mkdir()
    (tmp_path / "config/brand.json").write_text(json.dumps({"display_name": "全局名称"}), encoding="utf-8")
    message = api.get_messages("wxid_peer")["data"][0]
    assert message["avatar"] == "robot.jpg"
    assert message["sender_id"] == "wxid_bot"
    assert message["sender_name"] == "全局名称"
    api._contacts.self_profile = lambda: {}
    assert api.get_messages("wxid_peer")["data"][0]["avatar"] == ""
