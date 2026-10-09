import hashlib
import json
from pathlib import Path

import pytest

from source_inputs import repository_sources, verify_source_inputs


def inputs(root):
    vendor = root / "vendor"
    vendor.mkdir()
    (vendor / "module.py").write_bytes(b"value = 1\n")
    (vendor / "manifest.json").write_text(json.dumps({
        "schema_version": 1,
        "files": [{"path": "module.py", "sha256": hashlib.sha256(b"value = 1\n").hexdigest()}],
    }), encoding="utf-8")
    return vendor


def test_vendor_is_the_only_source_root(tmp_path):
    vendor = inputs(tmp_path)
    assert repository_sources(tmp_path) == vendor
    outside = tmp_path / "another-project"
    with pytest.raises(ValueError, match="禁止读取其他项目"):
        repository_sources(tmp_path, outside)
    assert not outside.exists()


@pytest.mark.parametrize("change", ["modify", "missing", "extra", "escape", "duplicate"])
def test_source_integrity_failures_stop_before_build(tmp_path, change):
    vendor = inputs(tmp_path)
    assert verify_source_inputs(tmp_path) == vendor
    if change == "modify":
        (vendor / "module.py").write_bytes(b"changed")
    elif change == "missing":
        (vendor / "module.py").unlink()
    elif change == "extra":
        (vendor / "unexpected.py").write_text("pass", encoding="utf-8")
    else:
        path = vendor / "manifest.json"
        manifest = json.loads(path.read_text("utf-8"))
        if change == "escape":
            manifest["files"][0]["path"] = "../another-project/module.py"
        else:
            manifest["files"].append(manifest["files"][0])
        path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError):
        verify_source_inputs(tmp_path)


def test_build_and_test_code_has_no_external_project_paths():
    root = Path(__file__).parents[1]
    # Redaction test strings are data, not Path arguments or source defaults.
    import ast
    forbidden = "e:/" + "project"
    for directory in ("tests", "scripts", "src/wechatrebot_manager"):
        for path in (root / directory).glob("*.py"):
            tree = ast.parse(path.read_text("utf-8-sig"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not node.args:
                    continue
                name = getattr(node.func, "id", getattr(node.func, "attr", ""))
                if name in {"Path", "open"} and isinstance(node.args[0], ast.Constant):
                    value = str(node.args[0].value).replace("\\", "/").casefold()
                    assert not value.startswith(forbidden), path.relative_to(root)
