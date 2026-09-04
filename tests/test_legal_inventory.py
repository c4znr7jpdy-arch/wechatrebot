import importlib.util
from pathlib import Path


def _builder():
    script = Path(__file__).parents[1] / "scripts" / "build_legal_inventory.py"
    spec = importlib.util.spec_from_file_location("build_legal_inventory", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_license_expression_uses_cyclonedx_expression() -> None:
    entry = _builder()._license_entry("Apache-2.0 OR BSD-3-Clause")

    assert entry == {"expression": "Apache-2.0 OR BSD-3-Clause"}


def test_unknown_license_is_not_reported_as_spdx_identifier() -> None:
    entry = _builder()._license_entry("NOASSERTION")

    assert entry == {"license": {"name": "NOASSERTION"}}
