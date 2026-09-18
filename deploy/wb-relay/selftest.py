# -*- coding: utf-8 -*-
"""
wb_relay 自测：用模拟上游验证代理的协议转换逻辑。

模拟上游严格复刻 workbuddy 的真实行为：
  - 首条消息不是 system -> 返回 {"code":11128,...}
  - stream != true      -> 返回 {"code":11101,...}
  - 否则返回带额外字段的 SSE 流

断言：
  1. 客户端发非流式且不带 system 消息 -> 代理补齐 system、强制流式，并把 SSE 聚合成 JSON
  2. 客户端发流式 -> 字段被归一化，内容完整，以 [DONE] 结束
  3. /v1/models 正常
"""

import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
RELAY = os.path.join(HERE, "wb_relay.py")
PY = sys.executable

MOCK_PORT = 18080
RELAY_PORT = 13010
MOCK = "http://127.0.0.1:%d" % MOCK_PORT
PROXY = "http://127.0.0.1:%d" % RELAY_PORT

seen = []          # 模拟上游收到的请求体
results = []


class MockUpstream(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n).decode("utf-8"))
        seen.append(body)

        msgs = body.get("messages") or []
        first = (msgs[0].get("role") if msgs and isinstance(msgs[0], dict) else "") or ""
        if first != "system":
            return self._json(400, {"code": 11128, "msg": "first message is not system prompt"})
        if body.get("stream") is not True:
            return self._json(400, {"code": 11101, "msg": "Non-stream chat request is currently not supported"})

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

        def chunk(delta, finish="", usage=None):
            return {
                "id": "cmb-selftest",
                "model": body.get("model") or "deepseek-v4.1-flash",
                "object": "chat.completion.chunk",
                "created": 1789440000,
                "choices": [{
                    "index": 0,
                    "delta": delta,
                    "logprobs": None,
                    "finish_reason": finish,
                }],
                "usage": usage,
            }

        frames = [
            chunk({"role": "assistant", "content": "", "reasoning_content": "",
                   "function_call": None, "refusal": "", "tool_calls": [], "extra_fields": None}),
            chunk({"content": "Hello", "reasoning_content": "", "refusal": "",
                   "function_call": None, "tool_calls": [], "extra_fields": None}),
            chunk({"content": " world", "reasoning_content": "", "refusal": "",
                   "function_call": None, "tool_calls": [], "extra_fields": None}),
            chunk({"role": "assistant", "content": "", "reasoning_content": "",
                   "function_call": {"name": "", "arguments": ""}, "refusal": "",
                   "tool_calls": [], "extra_fields": None},
                  finish="stop",
                  usage={"prompt_tokens": 9, "completion_tokens": 2, "total_tokens": 11,
                         "cached_tokens": 0, "credit": 0}),
        ]
        for f in frames:
            self.wfile.write(("data: " + json.dumps(f) + "\n\n").encode("utf-8"))
            self.wfile.flush()
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def _json(self, status, obj):
        b = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        self.wfile.write(b)


def post(url, payload):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 method="POST",
                                 headers={"Content-Type": "application/json",
                                          "Authorization": "Bearer fake-token"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def check(name, cond, detail=""):
    results.append((name, cond, detail))
    print("  [%s] %s%s" % ("PASS" if cond else "FAIL", name,
                           "" if cond else "  <- " + str(detail)))


def main():
    mock = ThreadingHTTPServer(("127.0.0.1", MOCK_PORT), MockUpstream)
    mock.daemon_threads = True
    threading.Thread(target=mock.serve_forever, daemon=True).start()
    print("模拟上游已启动 %s" % MOCK)

    env = dict(os.environ)
    env.update({
        "WB_HOST": "127.0.0.1",
        "WB_PORT": str(MOCK_PORT),
        "WB_SCHEME": "http",
        "WB_PATH": "/v2/chat/completions",
        "WB_LISTEN_HOST": "127.0.0.1",
        "WB_LISTEN_PORT": str(RELAY_PORT),
        "WB_MAX_RETRY": "2",
    })
    proc = subprocess.Popen([PY, "-u", RELAY], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    threading.Thread(target=lambda: [None for _ in proc.stdout], daemon=True).start()

    for _ in range(50):
        try:
            urllib.request.urlopen(PROXY + "/health", timeout=1).read()
            break
        except Exception:
            time.sleep(0.2)
    else:
        print("代理未启动"); proc.kill(); return 1

    print("\n--- 用例 1：非流式 + 不带 system 消息（最苛刻的组合）---")
    seen.clear()
    st, body = post(PROXY + "/v1/chat/completions", {
        "model": "deepseek-v4.1-flash",
        "stream": False,
        "messages": [{"role": "user", "content": "hi"}],
    })
    print("  HTTP %s  %s" % (st, body[:220]))
    check("返回 200", st == 200, st)
    try:
        j = json.loads(body)
    except Exception as e:
        j = {}
        check("响应是 JSON", False, e)
    check("object=chat.completion", j.get("object") == "chat.completion", j.get("object"))
    msg = (j.get("choices") or [{}])[0].get("message") or {}
    check("内容聚合完整", msg.get("content") == "Hello world", msg.get("content"))
    check("finish_reason=stop", (j.get("choices") or [{}])[0].get("finish_reason") == "stop")
    check("usage 透传", (j.get("usage") or {}).get("total_tokens") == 11, j.get("usage"))
    check("上游收到 stream=true", bool(seen) and seen[0].get("stream") is True,
          seen[0].get("stream") if seen else "no request")
    check("上游收到首条 system",
          bool(seen) and seen[0]["messages"][0]["role"] == "system",
          seen[0]["messages"][0] if seen else "no request")
    check("原始 user 消息保留",
          bool(seen) and seen[0]["messages"][-1].get("content") == "hi")

    print("\n--- 用例 2：流式 + 自带 system ---")
    seen.clear()
    st, body = post(PROXY + "/v1/chat/completions", {
        "model": "hy3",
        "stream": True,
        "messages": [{"role": "system", "content": "be terse"},
                     {"role": "user", "content": "hi"}],
    })
    lines = [l for l in body.splitlines() if l.startswith("data:")]
    print("  收到 %d 个 SSE 分片，HTTP %s" % (len(lines), st))
    check("HTTP 200", st == 200, st)
    check("以 [DONE] 结束", body.rstrip().endswith("data: [DONE]"))
    check("未重复注入 system", bool(seen) and len(seen[0]["messages"]) == 2,
          seen[0]["messages"] if seen else "no request")
    check("原有 system 未被覆盖",
          bool(seen) and seen[0]["messages"][0].get("content") == "be terse")
    text = ""
    stripped_ok = True
    for l in lines:
        d = l[5:].strip()
        if d == "[DONE]":
            continue
        c = json.loads(d)
        if "refusal" in c or "extra_fields" in c:
            stripped_ok = False
        for ch in c.get("choices") or []:
            text += (ch.get("delta") or {}).get("content") or ""
    check("流式内容完整", text == "Hello world", text)
    check("非标准字段已剔除", stripped_ok)

    print("\n--- 用例 3：/v1/models (GET) ---")
    with urllib.request.urlopen(PROXY + "/v1/models", timeout=10) as r:
        st, body = r.status, r.read().decode()
    print("  HTTP %s  %s" % (st, body[:180]))
    check("HTTP 200", st == 200, st)
    try:
        ids = [m["id"] for m in json.loads(body).get("data", [])]
    except Exception as e:
        ids = []
        check("可解析模型列表", False, e)
    check("含三个模型", ids == ["deepseek-v4.1-flash", "hy3", "hy4-preview"], ids)

    proc.kill()
    mock.shutdown()

    ok = sum(1 for _, c, _ in results if c)
    print("\n================ %d/%d 通过 ================" % (ok, len(results)))
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
