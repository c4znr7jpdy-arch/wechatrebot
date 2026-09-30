"""Exercise the deployed content-service extension without starting the robot."""

import base64
import json
import subprocess
import threading
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from test_short_drama_player_hardening import ROOT, _load_state_server, _patched_player


def test_tweet_page_store_auth_avatar_expiry_and_csp(tmp_path):
    checkout = _patched_player(tmp_path)
    subprocess.run(["git", "apply", str(ROOT / "patches/short-drama-player-twitter-pages.patch")],
                   cwd=checkout, check=True)
    module = _load_state_server(checkout, "hosted_tweet_test")
    store = module.StateStore(tmp_path / "state", 600, 100)
    server = ThreadingHTTPServer(("127.0.0.1", 0), module.Handler)
    server.state_store, server.api_token = store, "test-token"
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    base = f"http://127.0.0.1:{server.server_port}"
    payload = {"v": 1, "kind": "tweet", "html": "<p>译文和原文</p>",
               "avatar": base64.b64encode(b"\xff\xd8\xfftest\xff\xd9").decode()}
    try:
        request = Request(base + "/selections", data=json.dumps({"payload": payload}).encode(),
                          headers={"Content-Type": "application/json"})
        with pytest.raises(HTTPError) as denied:
            urlopen(request, timeout=3)
        assert denied.value.code == 401
        request.add_header("Authorization", "Bearer test-token")
        with urlopen(request, timeout=3) as response:
            token = json.load(response)["token"]
        with urlopen(base + "/twitter/" + token, timeout=3) as response:
            assert response.read().decode() == payload["html"]
            assert "sandbox; default-src 'none'" in response.headers["Content-Security-Policy"]
        with urlopen(base + "/twitter/" + token + "/avatar", timeout=3) as response:
            assert response.headers["Content-Type"] == "image/jpeg"
            assert response.read() == base64.b64decode(payload["avatar"])
        store.purge(float("inf"))
        with pytest.raises(HTTPError) as expired:
            urlopen(base + "/twitter/" + token, timeout=3)
        assert expired.value.code == 404
        for override in ({"avatar": "<svg/>"}, {"html": "x" * 524289}, {"avatar": 1}):
            assert module._validate_payload(payload | override)[0] is None
    finally:
        server.shutdown()
        server.server_close()
        worker.join()
