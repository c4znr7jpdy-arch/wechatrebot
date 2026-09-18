#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
workbuddy.ai -> OpenAI 兼容协议归一化代理

背景
----
上游 https://www.workbuddy.ai/v2/chat/completions 有两条硬约束：

  1. 首条消息必须是 system，否则返回 {"code":11128,"msg":"first message is not system prompt"}
  2. 必须 stream=true，否则返回 {"code":11101,"msg":"Non-stream chat request is currently not supported"}

new-api 无法满足这两条：
  - 它不会注入 system 消息；
  - 它会把客户端的 stream=false 原样转发；
  - 而且当上游返回 SSE 时，new-api 会把 info.IsStream 置为 true 并把 SSE 直接吐给
    非流式客户端（源码 relay/compatible_handler.go:198），不会聚合回 JSON。

本代理夹在 new-api 和上游之间，对外表现为一个行为正常的 OpenAI 端点：

  - 自动补齐缺失的 system 消息
  - 对上游强制 stream=true
  - 客户端要流式 -> 透传 SSE（字段已归一化）
  - 客户端要非流式 -> 把 SSE 聚合成单个 chat.completion JSON
  - 上游突发限流返回的 401/429/5xx 自动重试（实测上游 APISIX 网关会瞬时限流）
  - 凭证从请求的 Authorization 头透传给上游，因此 token 仍然由 new-api 渠道配置管理

只依赖 Python 标准库。
"""

import json
import os
import sys
import time
from http.client import HTTPConnection, HTTPSConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# --------------------------------------------------------------------------
# 配置（全部可用环境变量覆盖）
# --------------------------------------------------------------------------
UPSTREAM_HOST = os.environ.get("WB_HOST", "www.workbuddy.ai")
UPSTREAM_PATH = os.environ.get("WB_PATH", "/v2/chat/completions")
UPSTREAM_TLS = os.environ.get("WB_SCHEME", "https").lower() != "http"
UPSTREAM_PORT = int(os.environ.get("WB_PORT", "443" if UPSTREAM_TLS else "80"))

DEFAULT_SYSTEM_PROMPT = os.environ.get("WB_SYSTEM_PROMPT", "You are a helpful assistant.")
FALLBACK_TOKEN = os.environ.get("WB_TOKEN", "").strip()

# 按顺序尝试绑定，第一个成功的生效。
# 默认绑定 docker0 网关，容器可访问而外网不可访问。
LISTEN_CANDIDATES = [
    h.strip()
    for h in os.environ.get("WB_LISTEN_HOST", "172.18.0.1,0.0.0.0").split(",")
    if h.strip()
]
LISTEN_PORT = int(os.environ.get("WB_LISTEN_PORT", "3010"))

MAX_RETRY = int(os.environ.get("WB_MAX_RETRY", "3"))
RETRY_STATUS = {401, 403, 429, 500, 502, 503, 504}
CONNECT_TIMEOUT = float(os.environ.get("WB_CONNECT_TIMEOUT", "20"))
READ_TIMEOUT = float(os.environ.get("WB_READ_TIMEOUT", "600"))

MODELS = [
    m.strip()
    for m in os.environ.get("WB_MODELS", "deepseek-v4.1-flash,hy3,hy4-preview").split(",")
    if m.strip()
]

CHAT_PATHS = ("/v1/chat/completions", "/chat/completions", "/v1/completions")
MODEL_PATHS = ("/v1/models", "/models")


def log(msg):
    sys.stdout.write("[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    sys.stdout.flush()


# --------------------------------------------------------------------------
# 归一化
# --------------------------------------------------------------------------
def norm_usage(u):
    if not isinstance(u, dict):
        return None
    out = {}
    for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
        if k in u:
            out[k] = u[k]
    for k in ("prompt_tokens_details", "completion_tokens_details"):
        if isinstance(u.get(k), dict):
            out[k] = u[k]
    return out or None


def norm_chunk(c):
    """把上游的 chunk 归一化成标准 OpenAI 流式分片。"""
    out = {
        "id": c.get("id") or "",
        "object": "chat.completion.chunk",
        "created": c.get("created") or int(time.time()),
        "model": c.get("model") or "",
        "choices": [],
    }
    for ch in c.get("choices") or []:
        d = ch.get("delta") or {}
        nd = {}
        if d.get("role"):
            nd["role"] = d["role"]
        if d.get("content"):
            nd["content"] = d["content"]
        # reasoning_content 是上游推理模型的实际输出，国内客户端普遍识别
        if d.get("reasoning_content"):
            nd["reasoning_content"] = d["reasoning_content"]
        if d.get("tool_calls"):
            nd["tool_calls"] = d["tool_calls"]
        if d.get("function_call"):
            nd["function_call"] = d["function_call"]
        out["choices"].append(
            {
                "index": ch.get("index", 0),
                "delta": nd,
                "finish_reason": ch.get("finish_reason") or None,
            }
        )
    u = norm_usage(c.get("usage"))
    if u:
        out["usage"] = u
    return out


def iter_sse(resp):
    """逐行读取 SSE，产出解析后的 JSON 对象。"""
    while True:
        try:
            raw = resp.readline()
        except Exception:
            break
        if not raw:
            break
        line = raw.decode("utf-8", "replace").strip()
        if not line or not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            break
        try:
            obj = json.loads(data)
        except Exception:
            continue
        if isinstance(obj, dict):
            yield obj


def aggregate(chunks, fallback_model):
    """把流式分片聚合成单个非流式 chat.completion。"""
    cid = ""
    created = int(time.time())
    model = fallback_model
    content = []
    reasoning = []
    tool_calls = {}
    finish = None
    usage = None

    for c in chunks:
        if c.get("id"):
            cid = c["id"]
        if c.get("created"):
            created = c["created"]
        if c.get("model"):
            model = c["model"]
        u = norm_usage(c.get("usage"))
        if u:
            usage = u
        for ch in c.get("choices") or []:
            d = ch.get("delta") or {}
            if d.get("content"):
                content.append(d["content"])
            if d.get("reasoning_content"):
                reasoning.append(d["reasoning_content"])
            for tc in d.get("tool_calls") or []:
                idx = tc.get("index", 0)
                slot = tool_calls.setdefault(
                    idx,
                    {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
                )
                if tc.get("id"):
                    slot["id"] = tc["id"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    slot["function"]["name"] += fn["name"]
                if fn.get("arguments"):
                    slot["function"]["arguments"] += fn["arguments"]
            if ch.get("finish_reason"):
                finish = ch["finish_reason"]

    message = {"role": "assistant", "content": "".join(content)}
    if reasoning:
        message["reasoning_content"] = "".join(reasoning)
    if tool_calls:
        message["tool_calls"] = [tool_calls[k] for k in sorted(tool_calls)]

    out = {
        "id": cid,
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish or "stop"}],
    }
    if usage:
        out["usage"] = usage
    return out


# --------------------------------------------------------------------------
# 上游请求
# --------------------------------------------------------------------------
def extract_token(headers):
    auth = (headers.get("Authorization") or headers.get("authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        auth = auth[7:].strip()
    return auth or FALLBACK_TOKEN


def open_upstream(token, payload):
    """请求上游，对限流/网关抖动做重试。返回 (conn, resp)。"""
    last_err = None
    for attempt in range(MAX_RETRY):
        conn = None
        try:
            if UPSTREAM_TLS:
                conn = HTTPSConnection(UPSTREAM_HOST, UPSTREAM_PORT, timeout=CONNECT_TIMEOUT)
            else:
                conn = HTTPConnection(UPSTREAM_HOST, UPSTREAM_PORT, timeout=CONNECT_TIMEOUT)
            conn.timeout = READ_TIMEOUT
            conn.request(
                "POST",
                UPSTREAM_PATH,
                body=payload,
                headers={
                    "Authorization": "Bearer " + token,
                    "Content-Type": "application/json",
                    "Accept": "text/event-stream, application/json",
                    "User-Agent": "wb-relay/1.0",
                    "Connection": "close",
                },
            )
            resp = conn.getresponse()
            if resp.status in RETRY_STATUS and attempt < MAX_RETRY - 1:
                detail = resp.read()[:200]
                try:
                    conn.close()
                except Exception:
                    pass
                last_err = RuntimeError("upstream status %s" % resp.status)
                log("upstream %s (attempt %d/%d), retrying: %s"
                    % (resp.status, attempt + 1, MAX_RETRY, detail))
                time.sleep(0.8 * (2 ** attempt))
                continue
            return conn, resp
        except Exception as e:
            last_err = e
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
            log("upstream error (attempt %d/%d): %r" % (attempt + 1, MAX_RETRY, e))
            if attempt < MAX_RETRY - 1:
                time.sleep(0.8 * (2 ** attempt))
    raise last_err if last_err else RuntimeError("upstream request failed")


# --------------------------------------------------------------------------
# HTTP 处理
# --------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "wb-relay/1.0"

    def log_message(self, fmt, *args):  # 静音默认访问日志
        pass

    # ---------------- 响应工具 ----------------
    def _write_json(self, status, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        self.wfile.write(body)

    def _write_raw(self, status, body, ctype):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        self.wfile.write(body)

    def _error(self, status, message, code="upstream_error", etype="api_error"):
        self._write_json(status, {"error": {"message": message, "type": etype, "code": code}})

    # ---------------- GET ----------------
    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/health", "/", "/healthz"):
            self._write_json(
                200,
                {
                    "status": "ok",
                    "upstream": "%s://%s:%d%s"
                    % ("https" if UPSTREAM_TLS else "http", UPSTREAM_HOST, UPSTREAM_PORT, UPSTREAM_PATH),
                    "models": MODELS,
                    "force_stream": True,
                    "inject_system": DEFAULT_SYSTEM_PROMPT,
                },
            )
        elif path in MODEL_PATHS:
            now = int(time.time())
            self._write_json(
                200,
                {
                    "object": "list",
                    "data": [
                        {"id": m, "object": "model", "created": now, "owned_by": "workbuddy"}
                        for m in MODELS
                    ],
                },
            )
        else:
            self._error(404, "not found: %s" % path, "not_found", "invalid_request_error")

    # ---------------- POST ----------------
    def do_POST(self):
        path = self.path.split("?")[0]
        if path not in CHAT_PATHS:
            self._error(404, "unsupported path: %s" % path, "not_found", "invalid_request_error")
            return

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        raw = self.rfile.read(length) if length > 0 else b""

        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception as e:
            self._error(400, "invalid json body: %s" % e, "invalid_json", "invalid_request_error")
            return
        if not isinstance(body, dict):
            self._error(400, "body must be a json object", "invalid_json", "invalid_request_error")
            return

        messages = body.get("messages")
        if not isinstance(messages, list) or not messages:
            self._error(
                400, "messages is required and must be a non-empty array",
                "invalid_messages", "invalid_request_error",
            )
            return

        # --- 约束 1：首条必须是 system ---
        first_role = ""
        if isinstance(messages[0], dict):
            first_role = str(messages[0].get("role") or "")
        if first_role != "system":
            messages.insert(0, {"role": "system", "content": DEFAULT_SYSTEM_PROMPT})

        # --- 约束 2：上游必须流式 ---
        client_stream = bool(body.get("stream"))
        body["stream"] = True
        if isinstance(body.get("stream_options"), dict):
            body["stream_options"]["include_usage"] = True

        model = str(body.get("model") or "")
        token = extract_token(self.headers)
        if not token:
            self._error(
                401, "no credential provided (missing Authorization header)",
                "invalid_api_key", "authentication_error",
            )
            return

        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")

        try:
            conn, resp = open_upstream(token, payload)
        except Exception as e:
            log("upstream unreachable model=%s: %r" % (model, e))
            self._error(502, "upstream request failed: %s" % e)
            return

        try:
            ctype = (resp.headers.get("Content-Type") or "").lower()

            if resp.status != 200:
                err_body = resp.read().decode("utf-8", "replace")
                msg = err_body
                try:
                    j = json.loads(err_body)
                    if isinstance(j, dict):
                        msg = j.get("msg") or j.get("message") or err_body
                except Exception:
                    pass
                if resp.status == 401:
                    msg = "upstream rejected the credential (or rate-limited): %s" % str(msg)[:200]
                log("upstream %s model=%s: %s" % (resp.status, model, str(msg)[:200]))
                status = resp.status if 400 <= resp.status < 600 else 502
                self._error(status, str(msg)[:800])
                return

            if "text/event-stream" in ctype:
                if client_stream:
                    self._relay_stream(resp, model)
                else:
                    self._relay_aggregate(resp, model)
            else:
                # 上游直接给了 JSON（异常情况），原样回传
                self._write_raw(200, resp.read(), ctype or "application/json")
        finally:
            try:
                resp.close()
            except Exception:
                pass
            try:
                conn.close()
            except Exception:
                pass

    # ---------------- 流式 ----------------
    def _relay_stream(self, resp, model):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

        try:
            for chunk in iter_sse(resp):
                line = "data: " + json.dumps(norm_chunk(chunk), ensure_ascii=False) + "\n\n"
                self.wfile.write(line.encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as e:
            log("stream error model=%s: %r" % (model, e))

        try:
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except Exception:
            pass

    # ---------------- 非流式（聚合） ----------------
    def _relay_aggregate(self, resp, model):
        chunks = list(iter_sse(resp))
        self._write_json(200, aggregate(chunks, model))


def main():
    server = None
    for host in LISTEN_CANDIDATES:
        try:
            server = ThreadingHTTPServer((host, LISTEN_PORT), Handler)
            log("listening on %s:%d" % (host, LISTEN_PORT))
            break
        except OSError as e:
            log("bind %s:%d failed: %r" % (host, LISTEN_PORT, e))
    if server is None:
        log("FATAL: no listen address available")
        sys.exit(1)

    log("upstream  = %s://%s:%d%s"
        % ("https" if UPSTREAM_TLS else "http", UPSTREAM_HOST, UPSTREAM_PORT, UPSTREAM_PATH))
    log("models    = %s" % ",".join(MODELS))
    log("system    = %r" % DEFAULT_SYSTEM_PROMPT)
    log("retry     = %d attempts on %s" % (MAX_RETRY, sorted(RETRY_STATUS)))

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
