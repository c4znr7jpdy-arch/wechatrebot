from __future__ import annotations

import argparse
from email.parser import Parser
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tomllib
from urllib.parse import quote
import uuid


_LICENSE_CLASSIFIERS = {
    "Apache Software License": "Apache-2.0",
    "BSD License": "BSD License",
    "GNU Affero General Public License v3": "AGPL-3.0-only",
    "GNU General Public License v2 (GPLv2)": "GPL-2.0-only",
    "GNU General Public License v3 (GPLv3)": "GPL-3.0-only",
    "GNU Lesser General Public License v2 (LGPLv2)": "LGPL-2.0-only",
    "GNU Lesser General Public License v3 (LGPLv3)": "LGPL-3.0-only",
    "ISC License (ISCL)": "ISC",
    "MIT License": "MIT",
    "Mozilla Public License 2.0 (MPL 2.0)": "MPL-2.0",
    "Python Software Foundation License": "PSF-2.0",
}
_LICENSE_VALUES = {
    "Apache 2.0": "Apache-2.0",
    "Apache-2.0": "Apache-2.0",
    "BSD-2-Clause": "BSD-2-Clause",
    "BSD-3-Clause": "BSD-3-Clause",
    "ISC": "ISC",
    "MIT": "MIT",
    "MIT License": "MIT",
    "MPL-2.0": "MPL-2.0",
    "PSF-2.0": "PSF-2.0",
}
_LICENSE_FILE = re.compile(r"^(?:LICENSE|LICENCE|COPYING|NOTICE)(?:[._-].*)?$", re.I)
_FFMPEG_METADATA_FIELDS = (
    "version",
    "architecture",
    "distribution",
    "archive_filename",
    "archive_url",
    "archive_sha256",
    "license",
    "source_commit",
    "source_url",
    "build_tag",
    "build_source_url",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _canonical_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _metadata(path: Path):
    return Parser().parsestr((path / "METADATA").read_text(encoding="utf-8"))


def _license(metadata) -> str:
    expression = (metadata.get("License-Expression") or "").strip()
    if expression and expression.upper() != "UNKNOWN":
        return expression
    value = " ".join((metadata.get("License") or "").split())
    if value in _LICENSE_VALUES:
        return _LICENSE_VALUES[value]
    for classifier in metadata.get_all("Classifier", []):
        prefix = "License :: OSI Approved :: "
        if classifier.startswith(prefix):
            return _LICENSE_CLASSIFIERS.get(classifier[len(prefix) :], classifier)
    if value and value.upper() != "UNKNOWN" and len(value) <= 120:
        return value
    return "NOASSERTION"


def _source_url(metadata) -> str:
    preferred = ("Source", "Repository", "Homepage", "Home")
    urls: dict[str, str] = {}
    for value in metadata.get_all("Project-URL", []):
        if "," in value:
            label, url = value.split(",", 1)
            urls[label.strip()] = url.strip()
    for label in preferred:
        if urls.get(label):
            return urls[label]
    return (metadata.get("Home-page") or "").strip()


def _license_entry(value: str) -> dict[str, object]:
    if any(operator in value for operator in (" AND ", " OR ", " WITH ")):
        return {"expression": value}
    if value not in {"NOASSERTION", "Proprietary"} and re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9.+-]*", value
    ):
        return {"license": {"id": value}}
    return {"license": {"name": value}}


def _license_value(component: dict[str, object]) -> str:
    entry = component["licenses"][0]
    if "expression" in entry:
        return str(entry["expression"])
    node = entry["license"]
    return str(node.get("id") or node.get("name"))


def _license_documents(root: Path) -> tuple[Path, ...]:
    return tuple(
        path
        for path in sorted(root.rglob("*"))
        if path.is_file()
        and _LICENSE_FILE.fullmatch(path.name)
        and path.stat().st_size <= 2 * 1024 * 1024
    )


def _python_components(
    site_packages: Path,
    *,
    group: str,
) -> tuple[list[dict[str, object]], list[tuple[str, Path]]]:
    components: list[dict[str, object]] = []
    documents: list[tuple[str, Path]] = []
    for dist_info in sorted(site_packages.glob("*.dist-info")):
        metadata = _metadata(dist_info)
        name = (metadata.get("Name") or dist_info.name.split("-", 1)[0]).strip()
        version = (metadata.get("Version") or "UNKNOWN").strip()
        license_name = _license(metadata)
        component: dict[str, object] = {
            "type": "library",
            "group": group,
            "name": name,
            "version": version,
            "purl": f"pkg:pypi/{quote(_canonical_name(name))}@{quote(version)}",
            "licenses": [_license_entry(license_name)],
            "properties": [{"name": "wechatrebot:inventory_group", "value": group}],
        }
        source_url = _source_url(metadata)
        if source_url:
            component["externalReferences"] = [
                {"type": "website", "url": source_url}
            ]
        components.append(component)
        for document in _license_documents(dist_info):
            documents.append((f"{name} {version}", document))
    return components, documents


def _simple_yaml(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        match = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*):\s*[\"']?(.*?)[\"']?", line)
        if match:
            values[match.group(1)] = match.group(2).strip()
    return values


def _plugin_components(
    project_root: Path,
    feature_set: dict[str, object],
) -> tuple[list[dict[str, object]], list[tuple[str, Path]]]:
    components: list[dict[str, object]] = []
    documents: list[tuple[str, Path]] = []
    for feature in feature_set["features"]:
        module = feature["module"]
        plugin_root = project_root / "data" / "plugins" / module
        metadata = _simple_yaml(plugin_root / "metadata.yaml")
        license_files = _license_documents(plugin_root)
        license_name = "Proprietary"
        if feature["ownership"] == "third-party":
            license_name = "NOASSERTION"
            for license_file in license_files:
                heading = license_file.read_text(encoding="utf-8", errors="replace")[:500]
                if "GNU AFFERO GENERAL PUBLIC LICENSE" in heading.upper():
                    license_name = "AGPL-3.0-only"
                    break
                if "MIT LICENSE" in heading.upper():
                    license_name = "MIT"
                    break
        component: dict[str, object] = {
            "type": "application",
            "group": "delivered-plugin",
            "name": module,
            "version": metadata.get("version", "UNKNOWN").removeprefix("v"),
            "licenses": [_license_entry(license_name)],
            "properties": [
                {"name": "wechatrebot:ownership", "value": feature["ownership"]},
            ],
        }
        if metadata.get("repo"):
            component["externalReferences"] = [
                {"type": "vcs", "url": metadata["repo"]}
            ]
        components.append(component)
        for document in license_files:
            documents.append((f"{module} {component['version']}", document))
    return components, documents


def _validated_ffmpeg_metadata(
    runtime_root: Path,
    runtime_manifest: dict[str, object],
    definition: dict[str, object],
) -> dict[str, object]:
    metadata = runtime_manifest.get("ffmpeg")
    if not isinstance(metadata, dict):
        raise RuntimeError("runtime manifest does not include the pinned FFmpeg runtime")
    for field in _FFMPEG_METADATA_FIELDS:
        expected = definition.get(field)
        if not isinstance(expected, str) or not expected:
            raise RuntimeError(f"FFmpeg release definition is missing {field}")
        actual = metadata.get(field)
        if field == "archive_sha256" and isinstance(actual, str):
            actual, expected = actual.upper(), expected.upper()
        if actual != expected:
            raise RuntimeError(f"FFmpeg runtime does not match release definition: {field}")
    license_file = runtime_root / "ffmpeg" / "LICENSE.txt"
    if not license_file.is_file():
        raise RuntimeError("FFmpeg runtime license text is missing")
    files = metadata.get("files")
    license_records = [
        item
        for item in files if isinstance(item, dict) and item.get("path") == "LICENSE.txt"
    ] if isinstance(files, list) else []
    if len(license_records) != 1 or (
        str(license_records[0].get("sha256", "")).upper() != _sha256(license_file)
        or license_records[0].get("size") != license_file.stat().st_size
    ):
        raise RuntimeError("FFmpeg runtime license text does not match its file manifest")
    return metadata


def _ffmpeg_component(metadata: dict[str, object]) -> dict[str, object]:
    """Describe the exact redistributable FFmpeg archive in CycloneDX form."""
    return {
        "type": "application",
        "group": "runtime",
        "name": "FFmpeg",
        "version": str(metadata["version"]),
        "hashes": [
            {
                "alg": "SHA-256",
                "content": str(metadata["archive_sha256"]).upper(),
            }
        ],
        "licenses": [_license_entry(str(metadata["license"]))],
        "externalReferences": [
            {"type": "distribution", "url": str(metadata["archive_url"])},
            {"type": "vcs", "url": str(metadata["source_url"])},
            {"type": "build-system", "url": str(metadata["build_source_url"])},
        ],
        "properties": [
            {"name": "wechatrebot:inventory_group", "value": "runtime"},
            {
                "name": "wechatrebot:archive_filename",
                "value": str(metadata["archive_filename"]),
            },
            {
                "name": "wechatrebot:architecture",
                "value": str(metadata["architecture"]),
            },
            {
                "name": "wechatrebot:distribution",
                "value": str(metadata["distribution"]),
            },
            {
                "name": "wechatrebot:source_commit",
                "value": str(metadata["source_commit"]),
            },
            {
                "name": "wechatrebot:build_tag",
                "value": str(metadata["build_tag"]),
            },
        ],
    }


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _write_license_bundle(path: Path, documents: list[tuple[str, Path]]) -> None:
    sections: list[str] = [
        "WECHATREBOT THIRD-PARTY LICENSE TEXTS",
        "Generated from the exact runtime, manager build environment, and delivered plugins.",
        "Legal review is still required before commercial release.",
        "",
    ]
    seen: set[str] = set()
    for component, document in documents:
        content = document.read_text(encoding="utf-8", errors="replace").strip()
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest().upper()
        key = f"{component}\0{digest}"
        if key in seen:
            continue
        seen.add(key)
        sections.extend(
            [
                "=" * 78,
                f"COMPONENT: {component}",
                f"SOURCE FILE: {document.name}",
                f"TEXT SHA-256: {digest}",
                "=" * 78,
                content,
                "",
            ]
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(sections), encoding="utf-8")


def _write_notices(path: Path, components: list[dict[str, object]]) -> None:
    lines = [
        "# 第三方组件声明（待法务审核）",
        "",
        "本清单由锁定运行时、冻结管理器构建环境和实际交付插件生成。它是审查输入，",
        "不等同于许可证批准。`NOASSERTION`、强 copyleft 组件及本地修改必须单独关闭审查。",
        "",
        "特别提示：`astrbot_plugin_angel_heart 2.2.2` 检测为 AGPL-3.0-only。商业交付前",
        "必须确认完整对应源码、修改说明、许可证文本和网络交互场景下的源码提供方式。",
        "",
        "| 分组 | 组件 | 版本 | 许可证 |",
        "|---|---|---:|---|",
    ]
    for component in sorted(
        components,
        key=lambda item: (str(item.get("group")), str(item.get("name")).casefold()),
    ):
        license_name = _license_value(component)
        lines.append(
            f"| {component.get('group', '')} | {component['name']} | "
            f"{component.get('version', '')} | {license_name} |"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _bundled_node_inventory(site_packages: Path) -> tuple[list[dict[str, object]], list[tuple[str, Path]]]:
    driver = site_packages / "playwright" / "driver"
    executable = driver / "node.exe"
    license_path = driver / "LICENSE"
    if not executable.is_file() or not license_path.is_file():
        raise RuntimeError("Playwright bundled Node runtime or license text is missing")
    version = subprocess.check_output([str(executable), "--version"], text=True).strip().removeprefix("v")
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise RuntimeError("Playwright bundled Node version is invalid")
    component = {
        "type": "application", "group": "runtime", "name": "Node.js (Playwright bundled)",
        "version": version, "licenses": [_license_entry("MIT")],
        "hashes": [{"alg": "SHA-256", "content": _sha256(executable)}],
        "externalReferences": [{"type": "website", "url": "https://nodejs.org/"}],
        "properties": [{"name": "wechatrebot:purpose", "value": "Playwright and YouTube JavaScript runtime; no music sidecar"}],
    }
    return [component], [(f"Node.js {version} and bundled dependencies", license_path)]


def _shared_components(project_root: Path, feature_set: dict[str, object]) -> tuple[list[dict[str, object]], list[tuple[str, Path]]]:
    components = []
    documents = []
    for item in feature_set.get("shared_runtime", []):
        if not item.get("name"):
            continue
        source = project_root / item["source"]
        if not source.exists():
            raise RuntimeError(f"shared runtime source is missing: {item['source']}")
        components.append({
            "type": "library", "group": "shared-plugin-runtime", "name": item["name"],
            "version": item["version"], "licenses": [_license_entry(item["license"])],
            "properties": [{"name": "wechatrebot:purpose", "value": item["purpose"]}],
        })
        if item.get("license_file"):
            document = project_root / item["license_file"]
            if not document.is_file():
                raise RuntimeError(f"shared runtime license is missing: {item['name']}")
            documents.append((f"{item['name']} {item['version']}", document))
    return components, documents


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build release SBOM and NOTICE inputs.")
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--manager-site-packages", type=Path, required=True)
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--sbom", type=Path, required=True)
    parser.add_argument("--notices", type=Path, required=True)
    parser.add_argument("--licenses", type=Path, required=True)
    parser.add_argument("--review", type=Path, required=True)
    parser.add_argument(
        "--corresponding-source",
        type=Path,
        default=Path("legal/CORRESPONDING-SOURCE.md"),
    )
    args = parser.parse_args(argv)

    repository = Path(__file__).resolve().parents[1]
    runtime_root = args.runtime_root.resolve()
    runtime_site_packages = runtime_root / "python" / "Lib" / "site-packages"
    feature_set = json.loads(
        (repository / "release" / "feature-set.json").read_text(encoding="utf-8")
    )
    project = tomllib.loads(
        (repository / "pyproject.toml").read_text(encoding="utf-8")
    )
    release_version = str(project["project"]["version"])
    runtime_manifest = json.loads(
        (runtime_root / "runtime-manifest.json").read_text(encoding="utf-8")
    )
    ffmpeg_definition = json.loads(
        (repository / "release" / "ffmpeg-runtime.json").read_text(encoding="utf-8")
    )
    ffmpeg_manifest = _validated_ffmpeg_metadata(
        runtime_root, runtime_manifest, ffmpeg_definition
    )
    runtime_components, runtime_documents = _python_components(
        runtime_site_packages,
        group="runtime",
    )
    expected_count = runtime_manifest["wheelhouse"]["wheel_count"]
    if len(runtime_components) != expected_count:
        raise RuntimeError(
            f"runtime component count mismatch: {len(runtime_components)} != {expected_count}"
        )
    manager_components, manager_documents = _python_components(
        args.manager_site_packages.resolve(),
        group="manager-build",
    )
    plugin_components, plugin_documents = _plugin_components(
        args.project_root.resolve(),
        feature_set,
    )
    node_components, node_documents = _bundled_node_inventory(runtime_site_packages)
    shared_components, shared_documents = _shared_components(args.project_root.resolve(), feature_set)
    python_license = runtime_root / "python" / "LICENSE.txt"
    ffmpeg_license = runtime_root / "ffmpeg" / "LICENSE.txt"
    ffmpeg_component = _ffmpeg_component(ffmpeg_manifest)
    components = [
        {
            "type": "framework",
            "group": "runtime",
            "name": "CPython",
            "version": runtime_manifest["python"]["version"],
            "licenses": [{"license": {"id": "PSF-2.0"}}],
            "externalReferences": [
                {"type": "distribution", "url": runtime_manifest["python"]["source_url"]}
            ],
        },
        ffmpeg_component,
        *runtime_components,
        *manager_components,
        *plugin_components,
        *node_components,
        *shared_components,
    ]
    serial_seed = "|".join(
        [
            runtime_manifest["wheelhouse"]["aggregate_sha256"],
            str(ffmpeg_manifest["archive_sha256"]),
            *(f"{item['name']}@{item.get('version', '')}" for item in components),
        ]
    )
    sbom = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "serialNumber": f"urn:uuid:{uuid.uuid5(uuid.NAMESPACE_URL, serial_seed)}",
        "version": 1,
        "metadata": {
            "component": {
                "type": "application",
                "name": "WeChatReBot",
                "version": release_version,
            },
            "properties": [
                {
                    "name": "wechatrebot:runtime_aggregate_sha256",
                    "value": runtime_manifest["wheelhouse"]["aggregate_sha256"],
                },
                {"name": "wechatrebot:ffmpeg_included", "value": "true"},
                {
                    "name": "wechatrebot:ffmpeg_archive_sha256",
                    "value": str(ffmpeg_manifest["archive_sha256"]).upper(),
                },
                {"name": "wechatrebot:node_runtime_included", "value": "true"},
            ],
        },
        "components": components,
    }
    _write_json(args.sbom, sbom)
    _write_notices(args.notices, components)
    documents = [
        (f"CPython {runtime_manifest['python']['version']}", python_license),
        (f"FFmpeg {ffmpeg_manifest['version']}", ffmpeg_license),
        *runtime_documents,
        *manager_documents,
        *plugin_documents,
        *node_documents,
        *shared_documents,
    ]
    _write_license_bundle(args.licenses, documents)

    unresolved: list[str] = []
    copyleft: list[str] = []
    for component in components:
        license_name = _license_value(component)
        identity = f"{component['name']} {component.get('version', '')}".strip()
        if license_name == "NOASSERTION":
            unresolved.append(identity)
        if str(license_name).startswith(("AGPL-", "GPL-")):
            copyleft.append(identity)
    review = {
        "schema_version": 1,
        "status": "review-required",
        "runtime_component_count": len(runtime_components) + len(node_components) + 2,
        "shared_runtime_component_count": len(shared_components),
        "manager_build_component_count": len(manager_components),
        "delivered_plugin_count": len(plugin_components),
        "unresolved_license_components": unresolved,
        "strong_copyleft_components": copyleft,
        "required_human_approvals": [
            "open-source-license-counsel",
            "owner-source-rights",
            "customer-agreement",
        ],
        "artifacts": {
            "sbom": {"path": args.sbom.as_posix(), "sha256": _sha256(args.sbom)},
            "notices": {
                "path": args.notices.as_posix(),
                "sha256": _sha256(args.notices),
            },
            "license_texts": {
                "path": args.licenses.as_posix(),
                "sha256": _sha256(args.licenses),
            },
            "corresponding_source": {
                "path": args.corresponding_source.as_posix(),
                "sha256": _sha256(args.corresponding_source),
            },
        },
    }
    _write_json(args.review, review)
    print(json.dumps(review, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
