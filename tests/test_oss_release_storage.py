from __future__ import annotations

import json
from pathlib import Path
import subprocess

from wechatrebot_manager.payload import sha256_file

import scripts.oss_release_storage as oss


def test_project_oss_config_records_private_bucket_with_verified_upload_enabled() -> None:
    storage = oss.load_oss_release_storage()

    assert storage.enabled is True
    assert storage.bucket == "jiangxiaomei-releases-bj"
    assert storage.region == "cn-beijing"
    assert storage.endpoint == "oss-cn-beijing.aliyuncs.com"
    assert storage.object_prefix == "wechatrebot-updates"
    assert storage.public_url("downloads", "release file.zip") == (
        "https://download.xiuxianjyj.xin/wechatrebot-updates/"
        "downloads/release%20file.zip"
    )


def test_upload_uses_private_acl_and_verifies_size_and_sha_metadata(tmp_path: Path) -> None:
    local = tmp_path / "release.zip"
    local.write_bytes(b"release")
    digest = sha256_file(local)
    config = tmp_path / "oss.json"
    config.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "enabled": True,
                "bucket": "jiangxiaomei-releases-bj",
                "region": "cn-beijing",
                "endpoint": "oss-cn-beijing.aliyuncs.com",
                "object_prefix": "wechatrebot-updates",
                "public_base_url": "https://download.example.test/wechatrebot-updates",
            }
        ),
        encoding="utf-8",
    )
    storage = oss.load_oss_release_storage(config)
    commands: list[list[str]] = []
    stat_calls = 0

    def runner(argv, **_kwargs):
        nonlocal stat_calls
        commands.append(argv)
        if argv[1] == "stat":
            stat_calls += 1
            if stat_calls == 1:
                raise subprocess.CalledProcessError(2, argv)
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout=(
                    f"Content-Length : {local.stat().st_size}\n"
                    f"X-Oss-Meta-Sha256 : {digest}\n"
                ),
                stderr="",
            )
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    oss.upload_release_objects(
        storage,
        (
            oss.OssReleaseObject(
                local,
                "downloads",
                digest,
                "application/zip",
                "public,max-age=31536000,immutable",
            ),
        ),
        executable="ossutil.exe",
        runner=runner,
    )

    cp = next(command for command in commands if command[1] == "cp")
    assert cp[3] == "oss://jiangxiaomei-releases-bj/wechatrebot-updates/downloads/release.zip"
    assert cp[cp.index("--acl") + 1] == "private"
    assert cp[cp.index("--region") + 1] == "cn-beijing"
    assert cp[cp.index("--content-type") + 1] == "application/zip"
    assert cp[cp.index("--cache-control") + 1] == "public,max-age=31536000,immutable"
    assert cp[cp.index("--metadata") + 1] == f"sha256={digest}"
    assert stat_calls == 2
