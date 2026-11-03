# 分发迁移到 GitHub Release 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把客户安装包、完整载荷、在线更新包和签名稳定清单的发布目标从 OSS/CDN 与自建更新服务器换成本仓库的 GitHub Release，并让客户端内置清单地址指向固定的 `stable` 标签。

**Architecture:** 新增 `scripts/publish_github_release.py` 作为唯一发布入口：复用 `publish_full_update.py` 和 `publish_manager_update.py` 里的构建、签名与校验函数，把"上传到哪"替换为 `gh release` 命令，把"公网核对"替换为跟随 302 的 GitHub 资产校验，把"切换稳定清单"替换为覆盖上传 `stable` 标签的 `stable.json`。两个旧发布器退化为函数库，OSS、服务器 SSH、下载页代码与测试删除。

**Tech Stack:** Python 3.13、`gh` CLI 2.101、`packaging`、`cryptography`（Ed25519，已有）、pytest。所有脚本从仓库根以 `PYTHONPATH=src python -m pytest …` 运行测试。

**规格来源：** [docs/requirements/requirements-github-release-distribution.md](../../requirements/requirements-github-release-distribution.md)

---

## 文件结构

| 文件 | 职责 |
| --- | --- |
| `scripts/publish_github_release.py`（新建） | 发布入口：标签与资产地址、`gh` 包装、Release 创建/上传/发布、公网资产校验、稳定清单提升与回滚、`main` |
| `scripts/publish-github-release.ps1`（新建） | PowerShell 一键入口，转发全部参数 |
| `scripts/publish_full_update.py`（修改） | 只保留构建函数库：`build_full_update_artifacts`、客户包与载荷命名、`_release_from_output`；删除 OSS/服务器/下载页代码与 `main` |
| `scripts/publish_manager_update.py`（修改） | 只保留 `build_manager_update`、`_signing_key`、`_fetch_live_manifest`、`_require_safe_delta_base`、`_prompt_publish`；删除 SSH 发布与下载页版本读取 |
| `src/wechatrebot_manager/update-channel.json`（修改） | `manifest_url` 指向 GitHub `stable` 标签 |
| `installer/WeChatReBot.iss`（修改） | `AppUpdatesURL` 指向仓库 Releases 页 |
| `release/oss-publish.json`（修改） | `enabled:false` |
| 删除 | `scripts/oss_release_storage.py`、`scripts/publish_native_download.py`、`scripts/publish-full-update.ps1`、`scripts/publish-manager-update.ps1`、`scripts/publish-native-download.ps1`、`deploy/download-site/`、`deploy/nginx/wechatrebot-updates.conf`、`tests/test_oss_release_storage.py`、`tests/test_publish_native_download.py`、`tests/test_download_site.py` |
| `tests/test_publish_github_release.py`（新建） | 新入口的全部单元测试 |
| `tests/test_publish_full_update.py`、`tests/test_publish_manager_update.py`、`tests/test_updates.py`（修改） | 适配函数库化与新地址 |
| `README.md`、`docs/remote-updates.md`、`AGENTS.md`、`docs/release-acceptance-evidence.md`（修改） | 发布流程文档 |

仓库内 GitHub 仓库名固定为 `c4znr7jpdy-arch/wechatrebot`，运行时通过 `gh repo view` 读取，不硬编码进脚本。

---

### Task 0: 开分支并提交计划

**Files:**
- Create: `docs/superpowers/plans/2026-11-03-github-release-distribution.md`（本文件）

- [ ] **Step 1: 从最新 origin/master 开分支**

```bash
git fetch origin --quiet
git switch -c codex/feat-github-release-distribution-20261103 origin/master
```

- [ ] **Step 2: 提交计划**

```bash
git add docs/superpowers/plans/2026-11-03-github-release-distribution.md
git commit -m "docs: add GitHub Release distribution implementation plan"
```

---

### Task 1: 标签与资产地址

**Files:**
- Create: `scripts/publish_github_release.py`
- Test: `tests/test_publish_github_release.py`

- [ ] **Step 1: 写失败测试**

```python
"""GitHub Release publisher: naming, gh wrapper, verification, promotion, rollback."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys

import pytest


def _module():
    script = Path(__file__).parents[1] / "scripts" / "publish_github_release.py"
    spec = importlib.util.spec_from_file_location("publish_github_release", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


REPO = "c4znr7jpdy-arch/wechatrebot"


def test_release_tag_and_asset_urls_follow_github_download_layout() -> None:
    module = _module()
    assert module.release_tag("0.9.9") == "v0.9.9"
    assert module.release_tag("0.10.0.post1") == "v0.10.0.post1"
    assert module.asset_prefix(REPO, "v0.9.9") == f"https://github.com/{REPO}/releases/download/v0.9.9"
    assert module.asset_url(REPO, "v0.9.9", "payload.wrb") == (
        f"https://github.com/{REPO}/releases/download/v0.9.9/payload.wrb"
    )
    assert module.stable_manifest_url(REPO) == f"https://github.com/{REPO}/releases/download/stable/stable.json"
    with pytest.raises(ValueError):
        module.release_tag("not-a-version")
    with pytest.raises(ValueError):
        module.asset_prefix("owner only", "v1")
```

- [ ] **Step 2: 运行确认失败**

Run: `PYTHONPATH=src python -m pytest tests/test_publish_github_release.py -q -p no:cacheprovider`
Expected: FAIL，`FileNotFoundError`/`AssertionError`（脚本不存在）。

- [ ] **Step 3: 最小实现**

```python
"""Publish customer packages, payloads, update ZIPs and the signed stable manifest to GitHub Releases."""
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from packaging.version import InvalidVersion, Version

COMMERCIAL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = COMMERCIAL_ROOT / "scripts"
SOURCE_ROOT = COMMERCIAL_ROOT / "src"
for search_root in (SCRIPTS_ROOT, SOURCE_ROOT):
    if str(search_root) not in sys.path:
        sys.path.insert(0, str(search_root))

STABLE_TAG = "stable"
STABLE_MANIFEST_NAME = "stable.json"
_REPO_PATTERN = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")


def release_tag(version: str) -> str:
    try:
        return f"v{Version(version)}"
    except InvalidVersion as exc:
        raise ValueError(f"版本号无效：{version}") from exc


def _repo(repo: str) -> str:
    if not _REPO_PATTERN.fullmatch(repo):
        raise ValueError(f"GitHub 仓库名无效：{repo}")
    return repo


def asset_prefix(repo: str, tag: str) -> str:
    return f"https://github.com/{_repo(repo)}/releases/download/{tag}"


def asset_url(repo: str, tag: str, name: str) -> str:
    return f"{asset_prefix(repo, tag)}/{name}"


def stable_manifest_url(repo: str) -> str:
    return asset_url(repo, STABLE_TAG, STABLE_MANIFEST_NAME)
```

- [ ] **Step 4: 运行确认通过**

Run: `PYTHONPATH=src python -m pytest tests/test_publish_github_release.py -q -p no:cacheprovider`
Expected: `1 passed`

- [ ] **Step 5: 提交**

```bash
git add scripts/publish_github_release.py tests/test_publish_github_release.py
git commit -m "feat(release): add GitHub release tag and asset URL helpers"
```

---

### Task 2: 跟随跳转的公网资产校验

**Files:**
- Modify: `scripts/publish_github_release.py`
- Test: `tests/test_publish_github_release.py`

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_publish_github_release.py`：

```python
import hashlib
import io
import urllib.error


class _Response(io.BytesIO):
    def __init__(self, body: bytes, *, final_url: str, status: int = 200, length: int | None = None) -> None:
        super().__init__(body)
        self.status = status
        self._final_url = final_url
        self.headers = {"Content-Length": str(len(body) if length is None else length)}

    def geturl(self) -> str:
        return self._final_url

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def test_public_asset_check_follows_redirect_and_compares_size_and_sha256(monkeypatch) -> None:
    module = _module()
    body = b"release asset"
    url = f"https://github.com/{REPO}/releases/download/v0.9.9/asset.zip"
    final = "https://objects.githubusercontent.com/github-production-release-asset/1?X-Amz-Signature=abc"
    seen: list[str] = []

    def urlopen(request, *, timeout):
        seen.append(request.get_method())
        return _Response(body if request.get_method() == "GET" else b"", final_url=final, length=len(body))

    monkeypatch.setattr(module.urllib.request, "urlopen", urlopen)
    assert module.verify_public_asset(
        url, expected_size=len(body), expected_sha256=hashlib.sha256(body).hexdigest(), timeout=20,
    ) == final
    assert seen == ["HEAD", "GET"]


def test_public_asset_check_rejects_http_redirect_wrong_size_and_bad_digest(monkeypatch) -> None:
    module = _module()
    body = b"release asset"
    url = f"https://github.com/{REPO}/releases/download/v0.9.9/asset.zip"
    digest = hashlib.sha256(body).hexdigest()

    monkeypatch.setattr(module.urllib.request, "urlopen",
                        lambda request, *, timeout: _Response(body, final_url="http://insecure.example/asset.zip"))
    with pytest.raises(ValueError, match="HTTPS"):
        module.verify_public_asset(url, expected_size=len(body), expected_sha256=digest, timeout=20, attempts=1)

    monkeypatch.setattr(module.urllib.request, "urlopen",
                        lambda request, *, timeout: _Response(body, final_url=url, length=len(body) + 1))
    with pytest.raises(ValueError, match="大小"):
        module.verify_public_asset(url, expected_size=len(body), expected_sha256=digest, timeout=20, attempts=1)

    monkeypatch.setattr(module.urllib.request, "urlopen",
                        lambda request, *, timeout: _Response(body, final_url=url))
    with pytest.raises(ValueError, match="SHA-256"):
        module.verify_public_asset(url, expected_size=len(body), expected_sha256="0" * 64, timeout=20, attempts=1)


def test_public_asset_check_retries_transient_404_then_succeeds(monkeypatch) -> None:
    module = _module()
    body = b"release asset"
    url = f"https://github.com/{REPO}/releases/download/v0.9.9/asset.zip"
    calls = {"n": 0}

    def urlopen(request, *, timeout):
        calls["n"] += 1
        if calls["n"] == 1:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        return _Response(body if request.get_method() == "GET" else b"", final_url=url, length=len(body))

    slept: list[float] = []
    monkeypatch.setattr(module.urllib.request, "urlopen", urlopen)
    module.verify_public_asset(url, expected_size=len(body), expected_sha256=hashlib.sha256(body).hexdigest(),
                               timeout=20, attempts=3, delay=5.0, sleep=slept.append)
    assert slept == [5.0]
```

- [ ] **Step 2: 运行确认失败**

Run: `PYTHONPATH=src python -m pytest tests/test_publish_github_release.py -q -p no:cacheprovider`
Expected: 3 个新用例 FAIL，`AttributeError: ... has no attribute 'verify_public_asset'`。

- [ ] **Step 3: 最小实现**

追加到 `scripts/publish_github_release.py`：

```python
_USER_AGENT = "WeChatReBot-Publisher/2"


def _final_https_url(response, request_url: str) -> str:
    final = response.geturl()
    parsed = urllib.parse.urlsplit(final)
    if parsed.scheme.casefold() != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError(f"公网资产最终地址不是 HTTPS：{request_url}")
    if response.status != 200:
        raise ValueError(f"公网资产返回 HTTP {response.status}：{request_url}")
    return final


def _request(url: str, method: str = "GET") -> urllib.request.Request:
    return urllib.request.Request(
        url, method=method,
        headers={"Cache-Control": "no-cache", "User-Agent": _USER_AGENT, "Accept": "*/*"},
    )


def verify_public_asset(
    url: str, *, expected_size: int, expected_sha256: str, timeout: int,
    attempts: int = 3, delay: float = 5.0, sleep=time.sleep,
) -> str:
    """HEAD then GET the asset through GitHub's 302, comparing size and SHA-256. Returns the final URL."""
    failure: Exception | None = None
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(_request(url, "HEAD"), timeout=timeout) as response:
                final = _final_https_url(response, url)
                length = response.headers.get("Content-Length")
                if int(length or 0) != expected_size:
                    raise ValueError(f"公网资产大小与本地产物不一致：{url}")
            digest = hashlib.sha256()
            size = 0
            with urllib.request.urlopen(_request(url), timeout=timeout) as response:
                _final_https_url(response, url)
                while chunk := response.read(4 * 1024 * 1024):
                    size += len(chunk)
                    digest.update(chunk)
            if size != expected_size:
                raise ValueError(f"公网资产下载大小与本地产物不一致：{url}")
            if digest.hexdigest().lower() != expected_sha256.lower():
                raise ValueError(f"公网资产 SHA-256 与本地产物不一致：{url}")
            return final
        except urllib.error.HTTPError as exc:
            # A freshly uploaded asset can lag behind the release API for a few seconds.
            failure = ValueError(f"公网资产暂不可访问（HTTP {exc.code}）：{url}")
            if exc.code not in {403, 404} or attempt + 1 >= attempts:
                raise failure from exc
            sleep(delay)
    raise failure or ValueError(f"公网资产校验失败：{url}")
```

- [ ] **Step 4: 运行确认通过**

Run: `PYTHONPATH=src python -m pytest tests/test_publish_github_release.py -q -p no:cacheprovider`
Expected: `4 passed`

- [ ] **Step 5: 提交**

```bash
git add scripts/publish_github_release.py tests/test_publish_github_release.py
git commit -m "feat(release): verify GitHub assets through redirects by size and SHA-256"
```

---

### Task 3: `gh` 包装与 Release 操作

**Files:**
- Modify: `scripts/publish_github_release.py`
- Test: `tests/test_publish_github_release.py`

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_publish_github_release.py`：

```python
import subprocess


class _FakeGh:
    """Records gh argv and answers from a scripted table keyed by the subcommand prefix."""

    def __init__(self, answers: dict[tuple[str, ...], str | Exception] | None = None) -> None:
        self.calls: list[list[str]] = []
        self.answers = answers or {}

    def __call__(self, argv: list[str]) -> str:
        self.calls.append(list(argv))
        for prefix, answer in self.answers.items():
            if tuple(argv[: len(prefix)]) == prefix:
                if isinstance(answer, Exception):
                    raise answer
                return answer
        return ""


def test_gh_wrapper_runs_gh_and_returns_stdout(monkeypatch) -> None:
    module = _module()
    recorded: list[dict] = []

    def run(argv, **kwargs):
        recorded.append({"argv": argv, **kwargs})
        return subprocess.CompletedProcess(argv, 0, stdout='{"nameWithOwner":"o/r"}\n', stderr="")

    monkeypatch.setattr(module.subprocess, "run", run)
    assert module._gh(["repo", "view", "--json", "nameWithOwner"]) == '{"nameWithOwner":"o/r"}\n'
    assert recorded[0]["argv"][0] == "gh" and recorded[0]["check"] is True
    assert recorded[0]["capture_output"] is True and recorded[0]["encoding"] == "utf-8"
    assert module.repo_name_with_owner() == "o/r"


def test_release_lifecycle_uses_draft_then_publish_and_clobbers_assets(tmp_path: Path, monkeypatch) -> None:
    module = _module()
    notes = tmp_path / "notes.md"
    notes.write_text("- 修复", encoding="utf-8")
    asset = tmp_path / "a.zip"
    asset.write_bytes(b"a")
    gh = _FakeGh({("release", "view", "v0.9.9"): subprocess.CalledProcessError(1, "gh")})
    monkeypatch.setattr(module, "_gh", gh)

    assert module.release_exists(REPO, "v0.9.9") is False
    module.create_release(REPO, "v0.9.9", title="0.9.9", notes_file=notes, target="abc123", latest=True)
    module.upload_assets(REPO, "v0.9.9", [asset])
    module.publish_release(REPO, "v0.9.9")
    module.create_release(REPO, "stable", title="稳定清单", notes_file=notes, target="abc123", latest=False)

    assert gh.calls[1] == ["release", "create", "v0.9.9", "-R", REPO, "--draft", "--title", "0.9.9",
                           "--notes-file", str(notes), "--target", "abc123"]
    assert gh.calls[2] == ["release", "upload", "v0.9.9", str(asset), "-R", REPO, "--clobber"]
    assert gh.calls[3] == ["release", "edit", "v0.9.9", "-R", REPO, "--draft=false"]
    assert gh.calls[4][-2:] == ["--latest=false", "--target"] or "--latest=false" in gh.calls[4]


def test_release_exists_true_when_view_succeeds(monkeypatch) -> None:
    module = _module()
    gh = _FakeGh({("release", "view", "stable"): '{"tagName":"stable"}'})
    monkeypatch.setattr(module, "_gh", gh)
    assert module.release_exists(REPO, "stable") is True


def test_download_asset_places_file_in_directory(tmp_path: Path, monkeypatch) -> None:
    module = _module()
    gh = _FakeGh()
    monkeypatch.setattr(module, "_gh", gh)
    target = module.download_asset(REPO, "v0.9.8", "stable-0.9.8.json", tmp_path)
    assert target == tmp_path / "stable-0.9.8.json"
    assert gh.calls[0] == ["release", "download", "v0.9.8", "-R", REPO, "--pattern", "stable-0.9.8.json",
                           "--dir", str(tmp_path), "--clobber"]
```

- [ ] **Step 2: 运行确认失败**

Run: `PYTHONPATH=src python -m pytest tests/test_publish_github_release.py -q -p no:cacheprovider`
Expected: 4 个新用例 FAIL，`AttributeError`（`_gh`、`release_exists` 等不存在）。

- [ ] **Step 3: 最小实现**

追加到 `scripts/publish_github_release.py`：

```python
def _gh(argv: list[str]) -> str:
    command = ["gh", *argv]
    print("+", subprocess.list2cmdline(command), flush=True)
    completed = subprocess.run(command, cwd=COMMERCIAL_ROOT, check=True, capture_output=True,
                               text=True, encoding="utf-8", errors="replace")
    return completed.stdout


def repo_name_with_owner() -> str:
    payload = json.loads(_gh(["repo", "view", "--json", "nameWithOwner"]))
    return _repo(str(payload["nameWithOwner"]))


def ensure_gh_ready(repo: str) -> None:
    _gh(["auth", "status"])
    payload = json.loads(_gh(["repo", "view", repo, "--json", "viewerPermission"]))
    if payload.get("viewerPermission") not in {"ADMIN", "MAINTAIN", "WRITE"}:
        raise ValueError(f"当前 gh 账号对 {repo} 没有发布权限")


def release_exists(repo: str, tag: str) -> bool:
    try:
        _gh(["release", "view", tag, "-R", repo, "--json", "tagName"])
    except subprocess.CalledProcessError:
        return False
    return True


def create_release(repo: str, tag: str, *, title: str, notes_file: Path, target: str, latest: bool) -> None:
    argv = ["release", "create", tag, "-R", repo, "--draft", "--title", title,
            "--notes-file", str(notes_file), "--target", target]
    if not latest:
        argv.append("--latest=false")
    _gh(argv)


def upload_assets(repo: str, tag: str, paths: list[Path]) -> None:
    if not paths:
        return
    _gh(["release", "upload", tag, *(str(path) for path in paths), "-R", repo, "--clobber"])


def publish_release(repo: str, tag: str) -> None:
    _gh(["release", "edit", tag, "-R", repo, "--draft=false"])


def download_asset(repo: str, tag: str, name: str, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    _gh(["release", "download", tag, "-R", repo, "--pattern", name, "--dir", str(directory), "--clobber"])
    return directory / name
```

- [ ] **Step 4: 运行确认通过**

Run: `PYTHONPATH=src python -m pytest tests/test_publish_github_release.py -q -p no:cacheprovider`
Expected: `8 passed`

- [ ] **Step 5: 提交**

```bash
git add scripts/publish_github_release.py tests/test_publish_github_release.py
git commit -m "feat(release): wrap gh release create, upload, publish and download"
```

---

### Task 4: 构建函数库改用统一资产前缀

旧代码按 `{base}/downloads/{name}`、`{base}/releases/{name}` 拼地址；GitHub 所有资产都在同一个前缀下。把 `base_url` 换成 `asset_prefix`，地址恒为 `{asset_prefix}/{文件名}`。

**Files:**
- Modify: `scripts/publish_full_update.py`（`_customer_package_from_output`、`_native_customer_package_from_output`）
- Modify: `scripts/publish_manager_update.py`（`build_manager_update`）
- Test: `tests/test_publish_full_update.py`、`tests/test_publish_manager_update.py`

- [ ] **Step 1: 改现有测试为失败状态**

`tests/test_publish_full_update.py` 中 `test_customer_package_is_content_addressed_and_metadata_matches` 改为（删掉对 `_write_download_metadata` 的调用）：

```python
def test_customer_package_is_content_addressed_under_the_asset_prefix(tmp_path: Path) -> None:
    module = _publisher()
    package_root = tmp_path / "WeChatReBot-0.8.7-Windows-x64"
    original_archive = package_root.parent / f"{package_root.name}.zip"
    with zipfile.ZipFile(original_archive, "w") as archive:
        for name in ("WeChatReBot.exe", "payload.wrb", "DISTRIBUTION-MANIFEST.json", "START-HERE.txt"):
            archive.writestr(f"{package_root.name}/{name}", name)
    digest = sha256_file(original_archive)
    original_archive.with_name(original_archive.name + ".sha256").write_text(
        f"{digest}  {original_archive.name}\n", encoding="ascii")
    prefix = "https://github.com/o/r/releases/download/v0.8.7"

    package = module._customer_package_from_output(package_root, version="0.8.7", asset_prefix=prefix)

    assert digest[:12].lower() in package.archive_path.name
    assert package.package_url == f"{prefix}/{package.archive_path.name}"
    assert package.checksum_url == f"{prefix}/{package.checksum_path.name}"
    assert package.checksum_path.read_text(encoding="ascii").strip() == f"{digest}  {package.archive_path.name}"
    assert module._customer_package_from_output(package_root, version="0.8.7", asset_prefix=prefix) == package
```

`test_native_customer_package_uses_standard_setup_and_pinned_payload` 里把 `base_url="..."` 参数改成 `asset_prefix="https://github.com/o/r/releases/download/v0.9.8"`，并把对 `package.payload_url`、`package.package_url` 的断言改为 `f"{prefix}/{name}"` 形式（原断言若含 `/releases/` 或 `/downloads/` 子目录则改掉）。

`tests/test_publish_manager_update.py` 中 `test_build_manager_update_creates_small_signed_delta` 与 `test_build_manager_update_rejects_tampered_manager` 调用 `build_manager_update(...)` 的 `base_url=` 改为 `asset_prefix="https://github.com/o/r/releases/download/v0.8.8"`，`package_url` 断言改为 `f"{prefix}/{release.archive_path.name}"`。

- [ ] **Step 2: 运行确认失败**

Run: `PYTHONPATH=src python -m pytest tests/test_publish_full_update.py tests/test_publish_manager_update.py -q -p no:cacheprovider -k "asset_prefix or native_customer or build_manager_update"`
Expected: FAIL，`TypeError: ... unexpected keyword argument 'asset_prefix'`。

- [ ] **Step 3: 最小实现**

`scripts/publish_full_update.py`：

```python
def _customer_package_from_output(
    package_root: Path,
    *,
    version: str,
    asset_prefix: str,
    native: bool = False,
    manager_exe: Path | None = None,
    output_root: Path | None = None,
    build_installer: bool = True,
) -> BuiltCustomerPackage:
    prefix = asset_prefix.rstrip("/")
    if native:
        if manager_exe is None or output_root is None:
            raise ValueError("标准安装器构建缺少管理器或输出目录")
        return _native_customer_package_from_output(
            package_root, version=version, asset_prefix=prefix,
            manager_exe=manager_exe, output_root=output_root, build_installer=build_installer,
        )
    # …中间的归档校验与重命名逻辑保持不变…
    return BuiltCustomerPackage(
        version=version,
        archive_path=archive_path,
        checksum_path=checksum_path,
        package_url=f"{prefix}/{archive_path.name}",
        checksum_url=f"{prefix}/{checksum_path.name}",
        package_size=archive_path.stat().st_size,
        package_sha256=digest,
    )
```

`_native_customer_package_from_output(package_root, *, version, asset_prefix, manager_exe, output_root, build_installer)`：

```python
    payload_url = f"{asset_prefix}/{named_payload.name}"
    # …构建安装器与外层 ZIP 不变…
    return BuiltCustomerPackage(
        version=version,
        archive_path=archive,
        checksum_path=checksum,
        package_url=f"{asset_prefix}/{archive.name}",
        checksum_url=f"{asset_prefix}/{checksum.name}",
        package_size=archive.stat().st_size,
        package_sha256=package_digest,
        installer={
            "kind": "inno-online",
            "entrypoint": setup.name,
            "payload_url": payload_url,
            "payload_size": named_payload.stat().st_size,
            "payload_sha256": digest,
            "windows_code_signed": False,
        },
        payload_path=named_payload,
        payload_url=payload_url,
    )
```

`scripts/publish_manager_update.py` 的 `build_manager_update` 签名把 `base_url: str` 改为 `asset_prefix: str`，内部：

```python
    package_url = f"{asset_prefix.rstrip('/')}/{archive_name}"
```

- [ ] **Step 4: 运行确认通过**

Run: `PYTHONPATH=src python -m pytest tests/test_publish_full_update.py tests/test_publish_manager_update.py -q -p no:cacheprovider -k "asset_prefix or native_customer or build_manager_update"`
Expected: 全部 PASS。

- [ ] **Step 5: 提交**

```bash
git add scripts/publish_full_update.py scripts/publish_manager_update.py tests/test_publish_full_update.py tests/test_publish_manager_update.py
git commit -m "refactor(release): address customer and manager assets by a single prefix"
```

---

### Task 5: 把完整载荷流水线抽成函数

把 `publish_full_update.main` 里从"跑测试"到"生成客户包"的部分搬进 `build_full_update_artifacts`，供新入口调用。本任务只搬代码，不改行为。

**Files:**
- Modify: `scripts/publish_full_update.py`
- Test: `tests/test_publish_full_update.py`

- [ ] **Step 1: 写失败测试**

把 `test_full_update_entry_runs_complete_build_and_publish_pipeline` 替换为：

```python
def test_build_full_update_artifacts_runs_the_build_pipeline_in_order(
    tmp_path: Path, monkeypatch, vendor_inputs
) -> None:
    module = _publisher()
    root = tmp_path / "commercial"
    scripts = root / "scripts"
    source = vendor_inputs(root)
    (root / "artifacts").mkdir(parents=True)
    scripts.mkdir()
    (root / "src").mkdir()
    channel = UpdateChannel(manifest_url="https://github.com/o/r/releases/download/stable/stable.json",
                            channel="stable", key_id="test", public_key=b"0" * 32)
    commands: list[list[str]] = []
    monkeypatch.setattr(module, "COMMERCIAL_ROOT", root)
    monkeypatch.setattr(module, "SCRIPTS_ROOT", scripts)
    monkeypatch.setattr(module, "SOURCE_ROOT", root / "src")
    monkeypatch.setattr(module, "_run", lambda argv, **_kwargs: commands.append(argv))
    release = module.BuiltFullUpdate(
        version="0.8.7", archive_path=root / "artifacts" / "release.zip",
        manifest_path=root / "artifacts" / "stable.json",
        package_url="https://github.com/o/r/releases/download/v0.8.7/wechatrebot-0.8.7-update.zip",
        package_size=123, package_sha256="A" * 64,
    )
    customer = module.BuiltCustomerPackage(
        version="0.8.7", archive_path=root / "artifacts" / "customer.zip",
        checksum_path=root / "artifacts" / "customer.zip.sha256",
        package_url="https://github.com/o/r/releases/download/v0.8.7/customer.zip",
        checksum_url="https://github.com/o/r/releases/download/v0.8.7/customer.zip.sha256",
        package_size=8, package_sha256="B" * 64,
    )
    monkeypatch.setattr(module, "_release_from_output", lambda *_a, **_k: release)
    monkeypatch.setattr(module, "_customer_package_from_output", lambda *_a, **_k: customer)
    options = module.FullBuildOptions(
        source_root=source, private_key=root / "key.pem", output=root / "artifacts" / "publish",
        title="升级修复", notes=("修复更新回滚",), minimum_supported_version="0.7.0",
        build=None, mandatory=False, wheelhouse=None, offline=False,
        skip_tests=False, resume=False, refresh_manager=False,
    )

    built_release, built_customer = module.build_full_update_artifacts(
        options, version="0.8.7", channel=channel, previous_version="0.8.6",
        package_url=release.package_url, asset_prefix="https://github.com/o/r/releases/download/v0.8.7",
    )

    assert (built_release, built_customer) == (release, customer)
    invoked = ["pytest" if c[1:3] == ["-m", "pytest"] else Path(c[1]).name for c in commands if len(c) > 1]
    assert invoked == ["pytest", "validate_release_metadata.py", "build_manager.py", "build_runtime.py",
                       "build_legal_inventory.py", "validate_release_metadata.py", "build_staging.py",
                       "build_update_release.py", "build_distribution.py"]
    update_release = next(c for c in commands if "build_update_release.py" in c[1])
    assert update_release[update_release.index("--package-url") + 1] == release.package_url
    assert "--include" not in update_release
```

- [ ] **Step 2: 运行确认失败**

Run: `PYTHONPATH=src python -m pytest tests/test_publish_full_update.py -q -p no:cacheprovider -k build_full_update_artifacts`
Expected: FAIL，`AttributeError: ... 'FullBuildOptions'`。

- [ ] **Step 3: 实现（搬代码）**

在 `scripts/publish_full_update.py` 的 `BuiltCustomerPackage` 之后新增，并把 `main` 中对应段落剪切进来（`main` 在 Task 8 删除，此时先保留但改为调用本函数）：

```python
@dataclass(frozen=True, slots=True)
class FullBuildOptions:
    source_root: Path
    private_key: Path
    output: Path
    title: str
    notes: tuple[str, ...]
    minimum_supported_version: str
    build: int | None
    mandatory: bool
    wheelhouse: Path | None
    offline: bool
    skip_tests: bool
    resume: bool
    refresh_manager: bool


def build_full_update_artifacts(
    options: FullBuildOptions,
    *,
    version: str,
    channel: UpdateChannel,
    previous_version: str,
    package_url: str,
    asset_prefix: str,
) -> tuple[BuiltFullUpdate, BuiltCustomerPackage]:
    """Tests, metadata, manager, runtime, legal inventory, staging, signed update ZIP and customer package."""
    output = options.output
    output.mkdir(parents=True, exist_ok=True)
    build_env = os.environ.copy()
    build_env["PYTHONPATH"] = str(SOURCE_ROOT)
    if not options.skip_tests:
        _run([sys.executable, "-m", "pytest", "-q"], env=build_env)
        _run([sys.executable, str(SCRIPTS_ROOT / "validate_release_metadata.py"), "--root", str(COMMERCIAL_ROOT)],
             env=build_env)

    manager_build = output / "manager-build"
    runtime_build = output / "runtime-build"
    staging = output / "staging"
    update_output = output / "update"
    distribution_root = output / "distribution"
    if options.refresh_manager:
        for candidate in (manager_build, staging, update_output, distribution_root):
            try:
                candidate.relative_to(output)
            except ValueError as exc:
                raise RuntimeError(f"拒绝清理输出目录之外的路径：{candidate}") from exc
            if candidate.is_dir():
                shutil.rmtree(candidate)
            elif candidate.exists():
                candidate.unlink()
    distribution_root.mkdir(parents=True, exist_ok=True)
    customer_root = distribution_root / f"WeChatReBot-{version}-Windows-x64"
    rebuild = not options.resume or options.refresh_manager
    if rebuild:
        _run([sys.executable, str(SCRIPTS_ROOT / "build_manager.py"), "--output", str(manager_build),
              "--host-python", sys.executable])
    runtime_command = [sys.executable, str(SCRIPTS_ROOT / "build_runtime.py"), "--output", str(runtime_build),
                       "--host-python", sys.executable]
    if options.wheelhouse:
        runtime_command.extend(["--wheelhouse", str(options.wheelhouse.expanduser().resolve())])
    if options.offline:
        runtime_command.append("--offline")
    if not options.resume:
        _run(runtime_command)
    if rebuild:
        _run([sys.executable, str(SCRIPTS_ROOT / "build_legal_inventory.py"),
              "--runtime-root", str(runtime_build),
              "--manager-site-packages", str(manager_build / "build-venv" / "Lib" / "site-packages"),
              "--project-root", str(options.source_root),
              "--sbom", "release/sbom.cdx.json", "--notices", "legal/THIRD-PARTY-NOTICES.md",
              "--licenses", "legal/THIRD-PARTY-LICENSES.txt", "--review", "release/legal-review.json",
              "--corresponding-source", "legal/CORRESPONDING-SOURCE.md"])
        _run([sys.executable, str(SCRIPTS_ROOT / "validate_release_metadata.py"), "--root", str(COMMERCIAL_ROOT)],
             env=build_env)
        _run([sys.executable, str(SCRIPTS_ROOT / "build_staging.py"), "--source-root", str(options.source_root),
              "--runtime-root", str(runtime_build), "--manager-exe", str(manager_build / "dist" / "WeChatReBot.exe"),
              "--output", str(staging)])
    release_command = [sys.executable, str(SCRIPTS_ROOT / "build_update_release.py"), "--payload", str(staging),
                       "--output", str(update_output), "--private-key", str(options.private_key),
                       "--package-url", package_url, "--title", options.title,
                       "--minimum-supported-version", options.minimum_supported_version]
    if options.mandatory:
        release_command.append("--mandatory")
    if options.build is not None:
        release_command.extend(["--build", str(options.build)])
    if rebuild:
        for note in options.notes:
            release_command.extend(["--note", note])
        _run(release_command)
        _run([sys.executable, str(SCRIPTS_ROOT / "build_distribution.py"), "--payload", str(staging),
              "--output", str(customer_root)])
    else:
        print(f"正在复用已完成的完整构建产物：{output}", flush=True)

    release = _release_from_output(update_output, version=version, channel=channel,
                                   previous_version=previous_version)
    if release.package_url != package_url:
        raise ValueError("复用产物中的更新包地址与当前发布目标不一致；请使用新的输出目录重新构建，"
                         "或搭配 --refresh-manager 重建交付包。")
    customer_package = _customer_package_from_output(
        customer_root, version=version, asset_prefix=asset_prefix, native=True,
        manager_exe=manager_build / "dist" / "WeChatReBot.exe", output_root=output, build_installer=rebuild,
    )
    return release, customer_package
```

- [ ] **Step 4: 运行确认通过**

Run: `PYTHONPATH=src python -m pytest tests/test_publish_full_update.py -q -p no:cacheprovider`
Expected: 新用例 PASS；旧 `main` 相关用例此时若仍存在会 FAIL，下一任务删除。

- [ ] **Step 5: 提交**

```bash
git add scripts/publish_full_update.py tests/test_publish_full_update.py
git commit -m "refactor(release): extract build_full_update_artifacts from the full-update entry"
```

---

### Task 6: 稳定清单读取、提升与回滚

**Files:**
- Modify: `scripts/publish_github_release.py`
- Test: `tests/test_publish_github_release.py`

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_publish_github_release.py`：

```python
import base64
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from wechatrebot_manager.updates import UpdateChannel, canonical_manifest_bytes


def _signed_manifest(version: str, key: Ed25519PrivateKey, key_id: str, package_url: str) -> dict:
    manifest = {
        "schema_version": 1, "product": "wechatrebot-desktop", "channel": "stable", "version": version,
        "minimum_supported_version": "0.7.0", "published_at": "2026-11-03T00:00:00+00:00",
        "title": f"版本 {version}", "notes": [], "mandatory": False,
        "package": {"url": package_url, "format": "zip", "size": 3, "sha256": "A" * 64,
                    "payload_file_count": 1, "uncompressed_size": 3},
    }
    signature = key.sign(canonical_manifest_bytes(manifest))
    manifest["signature"] = {"algorithm": "ed25519", "key_id": key_id,
                             "value": base64.b64encode(signature).decode("ascii")}
    return manifest


def _channel(key: Ed25519PrivateKey) -> tuple[UpdateChannel, str]:
    public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    key_id = hashlib.sha256(public).hexdigest().upper()[:16]
    return UpdateChannel(manifest_url=f"https://github.com/{REPO}/releases/download/stable/stable.json",
                         channel="stable", key_id=key_id, public_key=public), key_id


def test_fetch_stable_manifest_returns_none_on_404(monkeypatch) -> None:
    module = _module()
    channel, _ = _channel(Ed25519PrivateKey.generate())

    def missing(_channel):
        raise urllib.error.HTTPError(channel.manifest_url, 404, "Not Found", {}, None)

    monkeypatch.setattr(module.publisher, "_fetch_live_manifest", missing)
    assert module.fetch_stable_manifest(channel) is None


def test_promote_manifest_uploads_version_copy_then_overwrites_stable(tmp_path: Path, monkeypatch) -> None:
    module = _module()
    key = Ed25519PrivateKey.generate()
    channel, key_id = _channel(key)
    manifest = _signed_manifest("0.9.9", key, key_id, module.asset_url(REPO, "v0.9.9", "u.zip"))
    manifest_path = tmp_path / "update" / "stable.json"
    manifest_path.parent.mkdir()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    gh = _FakeGh({("release", "view", "stable"): subprocess.CalledProcessError(1, "gh")})
    monkeypatch.setattr(module, "_gh", gh)
    monkeypatch.setattr(module.publisher, "_fetch_live_manifest", lambda _c: manifest)

    module.promote_manifest(REPO, "0.9.9", manifest_path, channel=channel, target="abc123",
                            work_root=tmp_path / "work", sleep=lambda _s: None)

    uploads = [c for c in gh.calls if c[:2] == ["release", "upload"]]
    assert uploads[0][2] == "v0.9.9" and uploads[0][3].endswith("stable-0.9.9.json")
    assert any(c[:3] == ["release", "create", "stable"] and "--latest=false" in c for c in gh.calls)
    assert uploads[1][2] == "stable" and uploads[1][3].endswith("stable.json") and "--clobber" in uploads[1]
    assert json.loads((tmp_path / "work" / "stable-0.9.9.json").read_text("utf-8")) == manifest


def test_promote_manifest_fails_when_public_manifest_differs(tmp_path: Path, monkeypatch) -> None:
    module = _module()
    key = Ed25519PrivateKey.generate()
    channel, key_id = _channel(key)
    manifest = _signed_manifest("0.9.9", key, key_id, module.asset_url(REPO, "v0.9.9", "u.zip"))
    other = _signed_manifest("0.9.8", key, key_id, module.asset_url(REPO, "v0.9.8", "u.zip"))
    manifest_path = tmp_path / "stable.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(module, "_gh", _FakeGh({("release", "view", "stable"): "{}"}))
    monkeypatch.setattr(module.publisher, "_fetch_live_manifest", lambda _c: other)
    with pytest.raises(ValueError, match="稳定清单"):
        module.promote_manifest(REPO, "0.9.9", manifest_path, channel=channel, target="abc123",
                                work_root=tmp_path / "work", attempts=2, sleep=lambda _s: None)


def test_rollback_stable_downloads_verifies_signature_and_republishes(tmp_path: Path, monkeypatch) -> None:
    module = _module()
    key = Ed25519PrivateKey.generate()
    channel, key_id = _channel(key)
    previous = _signed_manifest("0.9.8", key, key_id, module.asset_url(REPO, "v0.9.8", "u.zip"))

    def download(repo, tag, name, directory):
        assert (repo, tag, name) == (REPO, "v0.9.8", "stable-0.9.8.json")
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        path.write_text(json.dumps(previous), encoding="utf-8")
        return path

    gh = _FakeGh({("release", "view", "stable"): "{}"})
    monkeypatch.setattr(module, "_gh", gh)
    monkeypatch.setattr(module, "download_asset", download)
    monkeypatch.setattr(module.publisher, "_fetch_live_manifest", lambda _c: previous)
    module.rollback_stable(REPO, "0.9.8", channel=channel, work_root=tmp_path / "work", sleep=lambda _s: None)
    upload = next(c for c in gh.calls if c[:2] == ["release", "upload"])
    assert upload[2] == "stable" and upload[3].endswith("stable.json")

    tampered = dict(previous)
    tampered["title"] = "改过"
    monkeypatch.setattr(module, "download_asset",
                        lambda *a, **k: (tmp_path / "t.json").write_text(json.dumps(tampered), "utf-8") or tmp_path / "t.json")
    with pytest.raises(ValueError, match="签名"):
        module.rollback_stable(REPO, "0.9.8", channel=channel, work_root=tmp_path / "work2", sleep=lambda _s: None)
```

- [ ] **Step 2: 运行确认失败**

Run: `PYTHONPATH=src python -m pytest tests/test_publish_github_release.py -q -p no:cacheprovider`
Expected: 4 个新用例 FAIL（`fetch_stable_manifest`、`promote_manifest`、`rollback_stable`、`publisher` 不存在）。

- [ ] **Step 3: 最小实现**

在 `scripts/publish_github_release.py` 顶部导入区追加：

```python
import publish_manager_update as publisher
from wechatrebot_manager.payload import sha256_file
from wechatrebot_manager.updates import UpdateChannel, canonical_manifest_bytes, load_update_channel, parse_update_manifest, prepare_update
```

追加函数：

```python
def fetch_stable_manifest(channel: UpdateChannel) -> dict | None:
    try:
        return publisher._fetch_live_manifest(channel)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise


def _load_manifest(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError(f"清单根节点不是对象：{path}")
    return payload


def _verify_signed_manifest(payload: dict, channel: UpdateChannel, version: str) -> None:
    parsed = parse_update_manifest(payload, channel, current_version="0.0.0")
    if parsed is None or parsed.version != str(Version(version)):
        raise ValueError(f"清单签名校验失败或版本不是 {version}")


def verify_stable_manifest(channel: UpdateChannel, expected: dict, *, attempts: int = 10,
                           delay: float = 6.0, sleep=time.sleep) -> None:
    for attempt in range(attempts):
        live = fetch_stable_manifest(channel)
        if live is not None and canonical_manifest_bytes(live) == canonical_manifest_bytes(expected) \
                and live.get("signature") == expected.get("signature"):
            return
        if attempt + 1 < attempts:
            sleep(delay)
    raise ValueError("公网稳定清单与刚上传的清单不一致")


def ensure_stable_release(repo: str, *, target: str, work_root: Path) -> None:
    if release_exists(repo, STABLE_TAG):
        return
    notes = work_root / "stable-notes.md"
    work_root.mkdir(parents=True, exist_ok=True)
    notes.write_text("客户端读取的签名稳定清单。请勿手工修改此 Release 的资产。\n", encoding="utf-8")
    create_release(repo, STABLE_TAG, title="稳定清单", notes_file=notes, target=target, latest=False)
    publish_release(repo, STABLE_TAG)


def promote_manifest(repo: str, version: str, manifest_path: Path, *, channel: UpdateChannel, target: str,
                     work_root: Path, attempts: int = 10, delay: float = 6.0, sleep=time.sleep) -> None:
    """Keep a per-version signed copy, then overwrite stable/stable.json and read it back."""
    manifest = _load_manifest(manifest_path)
    _verify_signed_manifest(manifest, channel, version)
    work_root.mkdir(parents=True, exist_ok=True)
    tag = release_tag(version)
    versioned = work_root / f"stable-{Version(version)}.json"
    shutil.copyfile(manifest_path, versioned)
    upload_assets(repo, tag, [versioned])
    ensure_stable_release(repo, target=target, work_root=work_root)
    stable_copy = work_root / STABLE_MANIFEST_NAME
    shutil.copyfile(manifest_path, stable_copy)
    upload_assets(repo, STABLE_TAG, [stable_copy])
    verify_stable_manifest(channel, manifest, attempts=attempts, delay=delay, sleep=sleep)


def rollback_stable(repo: str, version: str, *, channel: UpdateChannel, work_root: Path,
                    attempts: int = 10, delay: float = 6.0, sleep=time.sleep) -> None:
    tag = release_tag(version)
    name = f"stable-{Version(version)}.json"
    downloaded = download_asset(repo, tag, name, work_root)
    manifest = _load_manifest(downloaded)
    _verify_signed_manifest(manifest, channel, version)
    stable_copy = work_root / STABLE_MANIFEST_NAME
    shutil.copyfile(downloaded, stable_copy)
    upload_assets(repo, STABLE_TAG, [stable_copy])
    verify_stable_manifest(channel, manifest, attempts=attempts, delay=delay, sleep=sleep)
```

- [ ] **Step 4: 运行确认通过**

Run: `PYTHONPATH=src python -m pytest tests/test_publish_github_release.py -q -p no:cacheprovider`
Expected: `12 passed`

- [ ] **Step 5: 提交**

```bash
git add scripts/publish_github_release.py tests/test_publish_github_release.py
git commit -m "feat(release): promote and roll back the signed stable manifest on GitHub"
```

---

### Task 7: `main`：完整载荷、管理器增量、回滚与一键入口

**Files:**
- Modify: `scripts/publish_github_release.py`
- Create: `scripts/publish-github-release.ps1`
- Test: `tests/test_publish_github_release.py`

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_publish_github_release.py`：

```python
def _fake_build(root: Path, module, version: str, prefix: str):
    update = root / "artifacts" / "out" / "update"
    update.mkdir(parents=True)
    archive = update / f"wechatrebot-{version}-update.zip"
    archive.write_bytes(b"zip")
    manifest = update / "stable.json"
    manifest.write_text("{}", encoding="utf-8")
    customer = root / "artifacts" / "out" / "customer.zip"
    customer.write_bytes(b"customer")
    checksum = customer.with_suffix(".zip.sha256")
    checksum.write_text("x", encoding="ascii")
    payload = root / "artifacts" / "out" / f"WeChatReBot-{version}-payload-abc.wrb"
    payload.write_bytes(b"payload")
    release = module.full.BuiltFullUpdate(
        version=version, archive_path=archive, manifest_path=manifest,
        package_url=f"{prefix}/{archive.name}", package_size=3, package_sha256=hashlib.sha256(b"zip").hexdigest(),
    )
    package = module.full.BuiltCustomerPackage(
        version=version, archive_path=customer, checksum_path=checksum,
        package_url=f"{prefix}/{customer.name}", checksum_url=f"{prefix}/{checksum.name}",
        package_size=8, package_sha256=hashlib.sha256(b"customer").hexdigest(),
        installer={"kind": "inno-online"}, payload_path=payload, payload_url=f"{prefix}/{payload.name}",
    )
    return release, package


def _common_main_patches(module, monkeypatch, tmp_path: Path, *, live_version: str | None):
    root = tmp_path / "commercial"
    (root / "artifacts").mkdir(parents=True)
    (root / "config" / "local").mkdir(parents=True)
    (root / "config" / "local" / "update-signing-private.pem").write_bytes(b"key")
    (root / "vendor").mkdir()
    key = Ed25519PrivateKey.generate()
    channel, _ = _channel(key)
    monkeypatch.setattr(module, "COMMERCIAL_ROOT", root)
    monkeypatch.setattr(module, "_project_version", lambda: "0.9.9")
    monkeypatch.setattr(module, "load_update_channel", lambda: channel)
    monkeypatch.setattr(module, "verify_source_inputs", lambda _root, source: source)
    monkeypatch.setattr(module.publisher, "_signing_key", lambda *_a: key)
    monkeypatch.setattr(module, "repo_name_with_owner", lambda: REPO)
    monkeypatch.setattr(module, "ensure_gh_ready", lambda _repo: None)
    monkeypatch.setattr(module, "_git_head", lambda: "abc123")
    monkeypatch.setattr(module, "fetch_stable_manifest",
                        lambda _c: None if live_version is None else {"version": live_version})
    monkeypatch.setattr(module.sys, "version_info", (3, 13, 0))
    monkeypatch.setattr(module.platform, "architecture", lambda: ("64bit", ""))
    return root, channel


def test_main_dry_run_builds_without_touching_github(tmp_path: Path, monkeypatch) -> None:
    module = _module()
    root, channel = _common_main_patches(module, monkeypatch, tmp_path, live_version="0.9.8")
    prefix = module.asset_prefix(REPO, "v0.9.9")
    built = _fake_build(root, module, "0.9.9", prefix)
    monkeypatch.setattr(module.full, "build_full_update_artifacts", lambda *_a, **_k: built)
    gh = _FakeGh()
    monkeypatch.setattr(module, "_gh", gh)
    assert module.main(["--dry-run", "--output", str(root / "artifacts" / "out"), "--title", "t"]) == 0
    assert gh.calls == []


def test_main_full_publish_creates_release_uploads_verifies_and_promotes(tmp_path: Path, monkeypatch) -> None:
    module = _module()
    root, channel = _common_main_patches(module, monkeypatch, tmp_path, live_version="0.9.8")
    prefix = module.asset_prefix(REPO, "v0.9.9")
    built = _fake_build(root, module, "0.9.9", prefix)
    monkeypatch.setattr(module.full, "build_full_update_artifacts", lambda *_a, **_k: built)
    gh = _FakeGh({("release", "view", "v0.9.9"): subprocess.CalledProcessError(1, "gh")})
    monkeypatch.setattr(module, "_gh", gh)
    verified: list[str] = []
    monkeypatch.setattr(module, "verify_public_asset", lambda url, **k: verified.append(url) or url)
    promoted: list[tuple] = []
    monkeypatch.setattr(module, "promote_manifest", lambda *a, **k: promoted.append((a, k)))
    smoked: list[str] = []
    monkeypatch.setattr(module, "smoke_update_from_github", lambda channel, release, root: smoked.append(release.version))

    assert module.main(["--output", str(root / "artifacts" / "out"), "--title", "t", "--yes"]) == 0

    create = next(c for c in gh.calls if c[:2] == ["release", "create"])
    assert create[2] == "v0.9.9" and "--draft" in create
    upload = next(c for c in gh.calls if c[:2] == ["release", "upload"])
    uploaded = {Path(p).name for p in upload[3:] if not p.startswith("-")}
    assert uploaded >= {"wechatrebot-0.9.9-update.zip", "customer.zip", "customer.zip.sha256",
                        "WeChatReBot-0.9.9-payload-abc.wrb", "wechatrebot-0.9.9-update.zip.sha256",
                        "WeChatReBot-0.9.9-payload-abc.wrb.sha256"}
    assert ["release", "edit", "v0.9.9", "-R", REPO, "--draft=false"] in gh.calls
    assert set(verified) == {built[0].package_url, built[1].package_url, built[1].payload_url}
    assert promoted and promoted[0][0][:2] == (REPO, "0.9.9")
    assert smoked == ["0.9.9"]


def test_main_refuses_version_not_above_live_stable(tmp_path: Path, monkeypatch) -> None:
    module = _module()
    root, _ = _common_main_patches(module, monkeypatch, tmp_path, live_version="0.9.9")
    with pytest.raises(SystemExit):
        module.main(["--output", str(root / "artifacts" / "out"), "--yes"])


def test_main_manager_only_uses_manager_delta_builder(tmp_path: Path, monkeypatch) -> None:
    module = _module()
    root, channel = _common_main_patches(module, monkeypatch, tmp_path, live_version="0.9.8")
    prefix = module.asset_prefix(REPO, "v0.9.9")
    commands: list[list[str]] = []
    monkeypatch.setattr(module, "_run", lambda argv, **_k: commands.append(argv))
    update = root / "artifacts" / "out" / "update"
    update.mkdir(parents=True)
    archive = update / "wechatrebot-0.9.9-manager-abc.zip"
    archive.write_bytes(b"m")
    manifest = update / "stable.json"
    manifest.write_text("{}", encoding="utf-8")
    delta = module.publisher.BuiltManagerUpdate(version="0.9.9", archive_path=archive, manifest_path=manifest,
                                                package_url=f"{prefix}/{archive.name}", package_size=1,
                                                package_sha256=hashlib.sha256(b"m").hexdigest())
    seen: dict = {}
    monkeypatch.setattr(module.publisher, "build_manager_update", lambda **k: seen.update(k) or delta)
    gh = _FakeGh({("release", "view", "v0.9.9"): subprocess.CalledProcessError(1, "gh")})
    monkeypatch.setattr(module, "_gh", gh)
    monkeypatch.setattr(module, "verify_public_asset", lambda url, **k: url)
    monkeypatch.setattr(module, "promote_manifest", lambda *a, **k: None)
    monkeypatch.setattr(module, "smoke_update_from_github", lambda *a, **k: None)

    assert module.main(["--manager-only", "--output", str(root / "artifacts" / "out"), "--yes", "--skip-tests"]) == 0
    assert Path(commands[0][1]).name == "build_manager.py"
    assert seen["asset_prefix"] == prefix
    upload = next(c for c in gh.calls if c[:2] == ["release", "upload"])
    assert any(p.endswith("wechatrebot-0.9.9-manager-abc.zip") for p in upload)


def test_main_manager_only_refuses_legacy_live_base(tmp_path: Path, monkeypatch) -> None:
    module = _module()
    root, _ = _common_main_patches(module, monkeypatch, tmp_path, live_version="0.8.6")
    with pytest.raises(SystemExit):
        module.main(["--manager-only", "--output", str(root / "artifacts" / "out"), "--yes", "--skip-tests"])


def test_main_rollback_calls_rollback_stable(tmp_path: Path, monkeypatch) -> None:
    module = _module()
    root, channel = _common_main_patches(module, monkeypatch, tmp_path, live_version="0.9.9")
    calls: list[tuple] = []
    monkeypatch.setattr(module, "rollback_stable", lambda repo, version, **k: calls.append((repo, version)))
    assert module.main(["--rollback", "0.9.8", "--yes"]) == 0
    assert calls == [(REPO, "0.9.8")]


def test_powershell_entry_forwards_all_arguments() -> None:
    script = (Path(__file__).parents[1] / "scripts" / "publish-github-release.ps1").read_text(encoding="utf-8")
    assert "publish_github_release.py" in script and "@args" in script and "py -3.13" in script
```

- [ ] **Step 2: 运行确认失败**

Run: `PYTHONPATH=src python -m pytest tests/test_publish_github_release.py -q -p no:cacheprovider`
Expected: 7 个新用例 FAIL（`main`、`full`、`_git_head`、`smoke_update_from_github` 不存在；ps1 文件不存在）。

- [ ] **Step 3: 实现**

`scripts/publish_github_release.py` 导入区追加：

```python
import publish_full_update as full
from source_inputs import verify_source_inputs
```

追加函数与 `main`：

```python
def _project_version() -> str:
    return full._project_version()


def _run(argv: list[str], *, env: dict[str, str] | None = None) -> None:
    full._run(argv, env=env)


def _git_head() -> str:
    completed = subprocess.run(["git", "rev-parse", "HEAD"], cwd=COMMERCIAL_ROOT, check=True,
                               capture_output=True, text=True, encoding="utf-8")
    return completed.stdout.strip()


def _write_checksum(path: Path) -> Path:
    checksum = path.with_name(path.name + ".sha256")
    checksum.write_text(f"{sha256_file(path)}  {path.name}\n", encoding="ascii")
    return checksum


def _write_notes(path: Path, *, title: str, notes: tuple[str, ...], version: str) -> Path:
    lines = [f"# {title.strip() or f'版本 {version}'}", ""]
    lines.extend(f"- {note.strip()}" for note in notes if note.strip())
    lines.extend(["", "安装：下载 `WeChatReBot-*-online-*.zip`，解压后运行其中的安装程序；安装时会从本 Release 下载 `payload.wrb` 并校验 SHA-256。",
                  "已安装的管理器会通过签名清单自动获得本次更新。"])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def smoke_update_from_github(channel: UpdateChannel, release, smoke_root: Path) -> None:
    """Act like an installed manager: read the manifest, download the package, check the digest."""
    live = fetch_stable_manifest(channel)
    parsed = parse_update_manifest(live or {}, channel, current_version="0.0.0")
    if parsed is None or parsed.version != release.version or parsed.package_sha256 != release.package_sha256:
        raise ValueError("公网稳定清单未指向刚发布的更新包")
    prepared = prepare_update(parsed, channel, smoke_root)
    if sha256_file(prepared.archive_path) != release.package_sha256:
        raise ValueError("从 GitHub 下载的更新包摘要不一致")
    if not (prepared.payload_root / "manager" / "WeChatReBot.exe").is_file():
        raise ValueError("从 GitHub 下载的更新包缺少管理器")


def _publish_assets(repo: str, tag: str, *, title: str, notes: tuple[str, ...], version: str,
                    assets: list[Path], work_root: Path, timeout: int) -> None:
    if release_exists(repo, tag):
        raise ValueError(f"Release {tag} 已存在；同版本不能重复发布，请提升版本号")
    checksums = [_write_checksum(path) for path in assets if not path.name.endswith(".sha256")]
    notes_file = _write_notes(work_root / "release-notes.md", title=title, notes=notes, version=version)
    create_release(repo, tag, title=f"{Version(version)} {title.strip()}".strip(), notes_file=notes_file,
                   target=_git_head(), latest=True)
    upload_assets(repo, tag, [*assets, *checksums])
    publish_release(repo, tag)
    for path in assets:
        if path.name.endswith(".sha256"):
            continue
        verify_public_asset(asset_url(repo, tag, path.name), expected_size=path.stat().st_size,
                            expected_sha256=sha256_file(path), timeout=timeout)
        print(f"GitHub 公网校验通过：{asset_url(repo, tag, path.name)}", flush=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="构建、签名并发布到 GitHub Release；稳定清单放在 stable 标签。")
    parser.add_argument("--source-root", type=Path, default=COMMERCIAL_ROOT / "vendor")
    parser.add_argument("--title", default="完整载荷更新")
    parser.add_argument("--note", action="append", default=[])
    parser.add_argument("--minimum-supported-version", default="0.7.0")
    parser.add_argument("--build", type=int)
    parser.add_argument("--mandatory", action="store_true")
    parser.add_argument("--private-key", type=Path,
                        default=COMMERCIAL_ROOT / "config" / "local" / "update-signing-private.pem")
    parser.add_argument("--wheelhouse", type=Path)
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--manager-build", type=Path, help="--manager-only 时复用已完成的管理器构建目录")
    parser.add_argument("--manager-only", action="store_true", help="只发布管理器增量包")
    parser.add_argument("--rollback", metavar="VERSION", help="把 stable 清单回滚到该版本的签名副本")
    parser.add_argument("--skip-tests", action="store_true")
    parser.add_argument("--skip-remote-smoke", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--refresh-manager", action="store_true")
    parser.add_argument("--yes", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if tuple(sys.version_info[:2]) != (3, 13) or platform.architecture()[0] != "64bit":
        parser.error("必须使用 Python 3.13 x64 执行发布脚本")
    if args.refresh_manager and not args.resume:
        parser.error("--refresh-manager 必须与 --resume 一起使用")
    channel = load_update_channel()
    repo = repo_name_with_owner()
    if not args.dry_run:
        ensure_gh_ready(repo)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")

    if args.rollback:
        version = str(Version(args.rollback))
        if not args.yes and input(f"将把稳定清单回滚到 {version}，输入 {version} 确认：").strip() != version:
            print("未确认，已取消回滚。")
            return 2
        rollback_stable(repo, version, channel=channel,
                        work_root=COMMERCIAL_ROOT / "artifacts" / f"rollback-{version}-{timestamp}")
        print(json.dumps({"rolled_back_to": version, "manifest_url": channel.manifest_url}, ensure_ascii=False))
        return 0

    private_key = args.private_key.expanduser().resolve()
    if not private_key.is_file():
        parser.error(f"更新签名私钥不存在：{private_key}")
    publisher._signing_key(private_key, channel)
    version = _project_version()
    live = fetch_stable_manifest(channel)
    live_version = str(Version(str(live["version"]))) if live else "0.0.0"
    if Version(version) < Version(live_version) or (Version(version) == Version(live_version) and not args.dry_run):
        parser.error(f"项目版本 {version} 必须高于线上稳定版本 {live_version}；请先提升版本号")
    if args.manager_only:
        try:
            publisher._require_safe_delta_base(live_version)
        except ValueError as exc:
            parser.error(str(exc))

    tag = release_tag(version)
    prefix = asset_prefix(repo, tag)
    output = (args.output or COMMERCIAL_ROOT / "artifacts" / f"publish-github-{version}-{timestamp}").expanduser().resolve()
    artifacts_root = (COMMERCIAL_ROOT / "artifacts").resolve()
    if output == artifacts_root or artifacts_root not in output.parents:
        parser.error("输出目录必须位于本项目 artifacts 目录内")
    if output.exists() and any(output.iterdir()) and not args.resume:
        parser.error(f"输出目录不是空目录：{output}")
    output.mkdir(parents=True, exist_ok=True)
    work_root = output / "github"

    if args.manager_only:
        if not args.skip_tests:
            env = os.environ.copy()
            env["PYTHONPATH"] = str(SOURCE_ROOT)
            _run([sys.executable, "-m", "pytest", "-q"], env=env)
        manager_build = args.manager_build.expanduser().resolve() if args.manager_build else output / "manager-build"
        if not args.manager_build:
            _run([sys.executable, str(SCRIPTS_ROOT / "build_manager.py"), "--output", str(manager_build),
                  "--host-python", sys.executable])
        release = publisher.build_manager_update(
            manager_build=manager_build, output=output / "update", version=version, private_key_path=private_key,
            channel=channel, asset_prefix=prefix, title=args.title, notes=tuple(args.note),
            minimum_supported_version=args.minimum_supported_version, build=args.build, mandatory=args.mandatory,
        )
        assets = [release.archive_path]
        customer = None
    else:
        source_root = verify_source_inputs(COMMERCIAL_ROOT, args.source_root)
        options = full.FullBuildOptions(
            source_root=source_root, private_key=private_key, output=output, title=args.title,
            notes=tuple(args.note), minimum_supported_version=args.minimum_supported_version, build=args.build,
            mandatory=args.mandatory, wheelhouse=args.wheelhouse, offline=args.offline, skip_tests=args.skip_tests,
            resume=args.resume, refresh_manager=args.refresh_manager,
        )
        release, customer = full.build_full_update_artifacts(
            options, version=version, channel=channel, previous_version=live_version,
            package_url=asset_url(repo, tag, f"wechatrebot-{version}-update.zip"), asset_prefix=prefix,
        )
        assets = [release.archive_path, customer.archive_path, customer.checksum_path]
        if customer.payload_path is not None:
            assets.append(customer.payload_path)

    summary = {
        "version": version, "tag": tag, "repo": repo,
        "assets": [{"name": p.name, "size": p.stat().st_size, "sha256": sha256_file(p)} for p in assets],
        "manifest": str(release.manifest_path), "package_url": release.package_url,
        "stable_manifest_url": stable_manifest_url(repo), "dry_run": args.dry_run,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    if args.dry_run:
        print("本地构建完成；--dry-run 未创建 Release。")
        return 0
    if not args.yes and not publisher._prompt_publish(version, release.package_size):
        print("未确认版本号，已取消发布。")
        return 2

    _publish_assets(repo, tag, title=args.title, notes=tuple(args.note), version=version, assets=assets,
                    work_root=work_root, timeout=channel.request_timeout_seconds)
    promote_manifest(repo, version, release.manifest_path, channel=channel, target=_git_head(), work_root=work_root)
    if not args.skip_remote_smoke:
        smoke_update_from_github(channel, release, artifacts_root / f"smoke-github-{version.replace('.', '')}")
    print(json.dumps({**summary, "published": True, "previous_version": live_version}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
```

`scripts/publish-github-release.ps1`：

```powershell
$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$publisher = Join-Path $PSScriptRoot "publish_github_release.py"
$publishExitCode = 1

Push-Location -LiteralPath $projectRoot
try {
    & py -3.13 $publisher @args
    $publishExitCode = $LASTEXITCODE
}
finally {
    Pop-Location
}

exit $publishExitCode
```

- [ ] **Step 4: 运行确认通过**

Run: `PYTHONPATH=src python -m pytest tests/test_publish_github_release.py -q -p no:cacheprovider`
Expected: `19 passed`

- [ ] **Step 5: 提交**

```bash
git add scripts/publish_github_release.py scripts/publish-github-release.ps1 tests/test_publish_github_release.py
git commit -m "feat(release): one-click GitHub Release publisher with manager-only and rollback"
```

---

### Task 8: 删除 OSS、服务器与下载页代码

**Files:**
- Delete: `scripts/oss_release_storage.py`、`scripts/publish_native_download.py`、`scripts/publish-full-update.ps1`、`scripts/publish-manager-update.ps1`、`scripts/publish-native-download.ps1`、`deploy/download-site/download.example.json`、`deploy/download-site/index.html`、`deploy/nginx/wechatrebot-updates.conf`、`tests/test_oss_release_storage.py`、`tests/test_publish_native_download.py`、`tests/test_download_site.py`
- Modify: `scripts/publish_full_update.py`、`scripts/publish_manager_update.py`、`tests/test_publish_full_update.py`、`tests/test_publish_manager_update.py`、`release/oss-publish.json`

- [ ] **Step 1: 删除文件**

```bash
git rm -q scripts/oss_release_storage.py scripts/publish_native_download.py scripts/publish-full-update.ps1 scripts/publish-manager-update.ps1 scripts/publish-native-download.ps1 deploy/download-site/download.example.json deploy/download-site/index.html deploy/nginx/wechatrebot-updates.conf tests/test_oss_release_storage.py tests/test_publish_native_download.py tests/test_download_site.py
```

- [ ] **Step 2: 精简 `publish_full_update.py`**

删除：`import oss_release_storage as oss_storage`、`import publish_manager_update as publisher`、`import platform`、`import shlex`、`import urllib.error`、`import urllib.request`、`import uuid`、`import argparse`、`from datetime import datetime`、`StagedDownloadSite`、`_write_download_metadata`、`_public_head`、`_verify_public_download`、`_public_download_page_is_hidden`、`_stage_download_site`、`_activate_download_metadata`、`main` 与 `if __name__ == "__main__"`。保留：`BuiltFullUpdate`、`BuiltCustomerPackage`、`FullBuildOptions`、`_project_version`、`_run`、`_release_from_output`、`_customer_package_from_output`、`_copy_or_link`、`_wrap_native_installer`、`_native_customer_package_from_output`、`build_full_update_artifacts`。文件首行加模块说明：

```python
"""Build helpers for full-payload releases. Publishing lives in publish_github_release.py."""
```

- [ ] **Step 3: 精简 `publish_manager_update.py`**

删除：`import oss_release_storage as oss_storage`、`DEFAULT_HOST`、`DEFAULT_REMOTE_ROOT`、`DEFAULT_BASE_URL`、`MAX_DOWNLOAD_METADATA_BYTES`、`_fetch_download_version`、`_published_version_floor`、`_safe_remote_inputs`、`_ssh`、`_scp`、`_public_package_head`、`_publish`、`main` 与 `if __name__ == "__main__"`，以及随之无用的 `import shlex`、`import uuid`、`import time`、`import urllib.parse`、`import urllib.error`、`import argparse`、`import platform`、`import shutil`、`from collections.abc import Callable`、`prepare_update` 导入。保留：`BuiltManagerUpdate`、`_project_version`、`_require_safe_delta_base`、`_load_json`、`_signing_key`、`_validated_manager`、`build_manager_update`、`_run`、`_fetch_live_manifest`、`_prompt_publish`。文件首行加：

```python
"""Build helpers for manager-only delta releases. Publishing lives in publish_github_release.py."""
```

- [ ] **Step 4: 删除对应测试**

`tests/test_publish_full_update.py` 删除 `test_public_cdn_download_checks_head_size_and_get_sha256`、`test_public_download_page_must_return_not_found`、`test_full_update_powershell_entry_forwards_all_arguments`、`test_oss_download_site_stage_does_not_copy_large_package_to_server`，并删掉不再使用的 `import io`、`import urllib.error`。
`tests/test_publish_manager_update.py` 删除 `test_remote_publish_inputs_reject_shell_metacharacters`、`test_publish_runs_after_switch_inside_manifest_rollback_boundary`、`test_publish_rolls_back_stable_manifest_when_after_switch_fails`、`test_publish_with_oss_only_copies_manifest_to_server`。保留 `test_manager_delta_rejects_legacy_stable_base`（它只用 `_require_safe_delta_base`）。

- [ ] **Step 5: `release/oss-publish.json` 置为未启用**

```json
{
  "schema_version": 1,
  "enabled": false,
  "bucket": "jiangxiaomei-releases-bj",
  "region": "cn-beijing",
  "endpoint": "oss-cn-beijing.aliyuncs.com",
  "object_prefix": "wechatrebot-updates",
  "public_base_url": "https://download.xiuxianjyj.xin/wechatrebot-updates",
  "retired_at": "2026-11-03",
  "replaced_by": "GitHub Release，见 docs/requirements/requirements-github-release-distribution.md"
}
```

- [ ] **Step 6: 运行确认通过**

Run: `python -m compileall -q scripts tests && PYTHONPATH=src python -m pytest tests/test_publish_full_update.py tests/test_publish_manager_update.py tests/test_publish_github_release.py tests/test_update_release_builder.py tests/test_update_status_builder.py tests/test_release_metadata.py -q -p no:cacheprovider`
Expected: 全部 PASS；`grep -rn "oss_storage\|publish_native_download\|_stage_download_site" scripts tests` 无结果。

- [ ] **Step 7: 提交**

```bash
git add -A scripts tests deploy release/oss-publish.json
git commit -m "refactor(release): retire OSS, update-server and download-site publishing"
```

---

### Task 9: 客户端清单地址与安装器地址

**Files:**
- Modify: `src/wechatrebot_manager/update-channel.json`、`installer/WeChatReBot.iss`
- Test: `tests/test_updates.py`、`tests/test_build_native_installer.py`

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_updates.py`（文件已导入 `load_update_channel`、`prepare_update`、`UpdateChannel`、`UpdateRelease`；若缺少则补）：

```python
def test_embedded_update_channel_points_at_the_github_stable_release() -> None:
    channel = load_update_channel()
    assert channel.manifest_url == "https://github.com/c4znr7jpdy-arch/wechatrebot/releases/download/stable/stable.json"
    assert channel.key_id == "59E10AD6E829BA64"


def test_download_accepts_https_redirect_to_github_asset_host(tmp_path: Path, monkeypatch) -> None:
    body = b"PK\x05\x06" + b"\x00" * 18  # empty zip
    release = UpdateRelease(
        version="0.9.9", title="t", notes=(), published_at="",
        package_url="https://github.com/o/r/releases/download/v0.9.9/wechatrebot-0.9.9-update.zip",
        package_size=len(body), package_sha256=hashlib.sha256(body).hexdigest().upper(),
        mandatory=False, build=None, minimum_supported_version="0.7.0", uncompressed_size=0,
    )
    channel = UpdateChannel(manifest_url="https://github.com/o/r/releases/download/stable/stable.json",
                            channel="stable", key_id="k", public_key=b"0" * 32)

    class Response(io.BytesIO):
        status = 200
        headers = {"Content-Length": str(len(body))}
        def geturl(self): return "https://objects.githubusercontent.com/github-production-release-asset/1?sig=x"
        def __enter__(self): return self
        def __exit__(self, *exc): self.close()

    monkeypatch.setattr(updates.urllib.request, "urlopen", lambda request, timeout: Response(body))
    (tmp_path / "install").mkdir()
    monkeypatch.setattr(updates, "_ensure_update_disk_space", lambda *a, **k: None)
    with pytest.raises(UpdateFailure):  # the empty zip has no payload/, so extraction fails after a successful download
        prepare_update(release, channel, tmp_path / "install")

    class Insecure(Response):
        def geturl(self): return "http://objects.githubusercontent.com/asset"

    monkeypatch.setattr(updates.urllib.request, "urlopen", lambda request, timeout: Insecure(body))
    with pytest.raises(UpdateError, match="HTTPS"):
        prepare_update(release, channel, tmp_path / "install")
```

执行者须按 `tests/test_updates.py` 现有导入风格补齐 `io`、`hashlib`、`updates` 模块别名、`UpdateFailure`、`UpdateError`（已有的用例 502 行附近定义了类似 `Response`，可复用其写法）。

追加到 `tests/test_build_native_installer.py`：

```python
def test_installer_update_link_points_at_github_releases() -> None:
    script = (Path(__file__).parents[1] / "installer" / "WeChatReBot.iss").read_text(encoding="utf-8")
    assert "AppUpdatesURL=https://github.com/c4znr7jpdy-arch/wechatrebot/releases" in script
    assert "wechatrebot-updates" not in script
```

- [ ] **Step 2: 运行确认失败**

Run: `PYTHONPATH=src python -m pytest tests/test_updates.py tests/test_build_native_installer.py -q -p no:cacheprovider -k "github or redirect"`
Expected: 清单地址用例与安装器用例 FAIL；跳转用例的 HTTPS 分支 PASS、其余按预期。

- [ ] **Step 3: 实现**

`src/wechatrebot_manager/update-channel.json`：

```json
{
  "schema_version": 1,
  "channel": "stable",
  "manifest_url": "https://github.com/c4znr7jpdy-arch/wechatrebot/releases/download/stable/stable.json",
  "key_id": "59E10AD6E829BA64",
  "public_key_base64": "r6VrZiQwHkEq8/vBf5qdWJ+P1kOcDWMIwe0ySpCCWLA=",
  "check_interval_seconds": 600,
  "request_timeout_seconds": 20,
  "max_package_bytes": 2147483648
}
```

`installer/WeChatReBot.iss` 第 40 行：

```ini
AppUpdatesURL=https://github.com/c4znr7jpdy-arch/wechatrebot/releases
```

- [ ] **Step 4: 运行确认通过**

Run: `PYTHONPATH=src python -m pytest tests/test_updates.py tests/test_build_native_installer.py tests/test_build_manager.py -q -p no:cacheprovider`
Expected: 全部 PASS（`test_build_manager` 覆盖内置通道随管理器打包）。

- [ ] **Step 5: 提交**

```bash
git add src/wechatrebot_manager/update-channel.json installer/WeChatReBot.iss tests/test_updates.py tests/test_build_native_installer.py
git commit -m "feat(client): read the stable manifest from the GitHub stable release"
```

---

### Task 10: 文档

**Files:**
- Modify: `README.md`、`docs/remote-updates.md`、`AGENTS.md`、`docs/release-acceptance-evidence.md`

- [ ] **Step 1: README.md**

把"日常功能更新"与"可重复构建"中两个发布命令块（`publish-manager-update.ps1`、`publish-full-update.ps1`）替换为：

````markdown
仅修改管理器、插件中心或管理器内置页面时，使用一键入口发布管理器增量：

```powershell
.\scripts\publish-github-release.ps1 --manager-only --title '插件配置更新' --note '修复高级配置页面'
```

运行时、AstrBot 插件本体、桥接程序、hook 有变化时，发布完整载荷：

```powershell
.\scripts\publish-github-release.ps1 --source-root '.\vendor' --title '完整载荷稳定性更新' --note '修复旧版管理器更新回滚' --yes
```

两者都会在本地完成测试、构建、签名，然后在 GitHub 创建 `v<版本>` Release 并上传客户安装 ZIP、载荷、更新包和 `.sha256`，从公网逐个校验后把签名清单覆盖到固定的 `stable` Release，再按客户端逻辑做一次下载冒烟。`--dry-run` 只构建不发布；`--rollback <版本>` 把稳定清单回滚到该版本的签名副本。客户从仓库 Releases 页下载安装包。
````

把"项目版本必须已经高于线上稳定版"一句保留；删除"OSS/CDN"字样。

- [ ] **Step 2: docs/remote-updates.md**

- "首次绑定"一节的清单地址改为 `https://github.com/c4znr7jpdy-arch/wechatrebot/releases/download/stable/stable.json`，删除"服务器发布目录为 /srv/wechatrebot-updates…下载页默认返回 404…"整段。
- "每次发布"两小节替换为 README 中同样的两段命令与说明；补一句：Release 资产地址会 302 到 `objects.githubusercontent.com`，客户端与发布器都跟随跳转并校验最终地址为 HTTPS。
- 删除"OSS/CDN 大文件分发"整节，替换为一节"GitHub Release 布局"，内容取自规格第 2 节的表格。
- "客户端安全与回滚"与"发布纪律"保留；回滚一句改为"回滚使用 `publish-github-release.ps1 --rollback <版本>`"。

- [ ] **Step 3: AGENTS.md 第 31 行**

替换为：

```markdown
- 0.9.7 及更早版本保持历史发布渠道不变。从 0.9.9 起，完整载荷、客户安装包、在线更新包和签名稳定清单只发布到本仓库的 GitHub Release（版本标签 `v<版本>`，稳定清单在固定标签 `stable`）；发布前从公网资产地址跟随跳转核对大小与 SHA-256，核对失败不得更新稳定清单。OSS/CDN 与自建更新服务器已退役，不得回退。
```

- [ ] **Step 4: docs/release-acceptance-evidence.md 第 42 行**

把"版本化 ZIP 先上传并校验，`stable.json` 最后原子替换；再验证线上 ZIP、签名清单和立即前一版本升级"改为"版本 Release 的全部资产先上传并从公网校验，`stable` 标签的 `stable.json` 最后覆盖；再验证公网清单验签、更新包下载和立即前一版本升级"。

- [ ] **Step 5: 校验与提交**

Run: `grep -rn "publish-full-update.ps1\|publish-manager-update.ps1\|OSS/CDN\|download.json" README.md docs/remote-updates.md AGENTS.md docs/release-acceptance-evidence.md`
Expected: 无结果（规格文档与 `release/oss-publish.json` 允许保留历史字样）。

```bash
git add README.md docs/remote-updates.md AGENTS.md docs/release-acceptance-evidence.md
git commit -m "docs: publish through GitHub Releases instead of OSS and the update server"
```

---

### Task 11: 整体验证与 PR

- [ ] **Step 1: 定向测试**

Run: `PYTHONPATH=src python -m pytest tests/test_publish_github_release.py tests/test_publish_full_update.py tests/test_publish_manager_update.py tests/test_updates.py tests/test_update_release_builder.py tests/test_update_status_builder.py tests/test_build_native_installer.py tests/test_build_manager.py tests/test_release_metadata.py tests/test_self_test.py -q -p no:cacheprovider`
Expected: 全部 PASS。

- [ ] **Step 2: 静态检查**

Run: `python -m compileall -q src scripts tests && python scripts/validate_release_metadata.py && git diff --check origin/master...HEAD`
Expected: 无错误；元数据校验 `"ok": true`。

- [ ] **Step 3: 本地干跑入口（不发布）**

Run: `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/publish-github-release.ps1 --dry-run --skip-tests --output artifacts/publish-github-dryrun`
Expected: 这会真实构建管理器、运行时与载荷，耗时约 10 分钟，输出 JSON 摘要并打印"--dry-run 未创建 Release"，`gh` 未被调用。若维护者未确认可执行完整构建，本步改为 `py -3.13 scripts/publish_github_release.py --help` 并记录"干跑待维护者确认后执行"。

- [ ] **Step 4: 推送并建 PR**

```bash
git push -u origin codex/feat-github-release-distribution-20261103
gh pr create --base master --head codex/feat-github-release-distribution-20261103 --title "feat: publish releases through GitHub Releases" --body-file -
```

PR 正文按 `.github/pull_request_template.md`：改动内容（三段：新入口、函数库化与退役、客户端与文档）；自检勾选；验证结果列出 Step 1–3 的命令与结果；风险与后续写明"首次正式发布须维护者授权执行 `publish-github-release.ps1`，发布后 `stable` 标签即对所有已安装客户端生效；国内下载可达性按规格由维护者承担；回滚用 `--rollback`"。不合并。

---

## 自检记录

- 规格覆盖：§2 布局 → Task 1/6/7；§3 流程 1–6 与回滚 → Task 5/7/6；`--manager-only` 与 0.8.7 兼容检查 → Task 7；§4 客户端与安装器 → Task 9；发布端跟随跳转 → Task 2；文档 → Task 10；§5 退役与 `oss-publish.json` → Task 8；`gh auth` 检查 → Task 3/7。
- 占位扫描：无 TBD/TODO；每个代码步骤给出完整代码或精确的删改清单。
- 命名一致性：`asset_prefix`（Task 4/5/7）、`build_full_update_artifacts` 与 `FullBuildOptions`（Task 5/7）、`promote_manifest`/`rollback_stable`/`verify_stable_manifest`/`fetch_stable_manifest`（Task 6/7）、`verify_public_asset`（Task 2/7）、`release_exists`/`create_release`/`upload_assets`/`publish_release`/`download_asset`（Task 3/6/7）、`smoke_update_from_github`/`_git_head`（Task 7 测试与实现）一致。
