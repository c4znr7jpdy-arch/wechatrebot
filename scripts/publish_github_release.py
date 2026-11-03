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
