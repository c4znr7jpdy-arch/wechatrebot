"""Numeric blanks must not become different values in offline and online saves."""
import json
from unittest.mock import Mock

import pytest

from test_plugin_center import _install
from wechatrebot_manager.plugin_center import PluginCenterError, PluginCenterService, _json_revision
from wechatrebot_manager.plugin_configuration import _customer_schema, _validate_config


@pytest.fixture
def numeric_service(tmp_path):
    install, public_id = _install(tmp_path)
    schema_path = install / "app/astrbot/data/plugins/sample_configurable_feature/_conf_schema.json"
    schema = json.loads(schema_path.read_text("utf-8"))
    schema["numbers"] = {"type": "object", "items": {
        "quantity": {"type": "int", "description": "返回数量", "default": 3},
        "ratio": {"type": "float", "description": "采样比例", "default": 0.5},
        "optional": {"type": "int", "description": "可选上限", "default": None},
    }}
    schema_path.write_text(json.dumps(schema), "utf-8")
    config_path = install / "app/astrbot/data/config/sample_configurable_feature_config.json"
    service = PluginCenterService(install)
    service.engine.is_available = lambda: False

    def get_config(_module):
        config = json.loads(config_path.read_text("utf-8"))
        return {"config": config, "revision": _json_revision(config)}

    def save_config(_module, config, _revision):
        config_path.write_text(json.dumps(config), "utf-8")
        return {"verified": True, "state": "applied", "revision": _json_revision(config)}

    service.engine.get_config = get_config
    service.engine.save_config = Mock(side_effect=save_config)
    service.engine.plugin_states = lambda: {"sample_configurable_feature": True}
    return service, public_id, config_path


@pytest.mark.parametrize("online", [False, True])
@pytest.mark.parametrize("key,label", [("quantity", "返回数量"), ("ratio", "采样比例")])
def test_empty_numeric_value_is_rejected_before_any_write(numeric_service, online, key, label):
    service, public_id, path = numeric_service
    service.engine.is_available = lambda: online
    detail = service.detail(public_id)
    detail["config"]["numbers"][key] = None
    before = path.read_bytes()
    with pytest.raises(PluginCenterError, match=f"{label}.*不能为空"):
        service.save(public_id, detail["config"], detail["revision"])
    service.engine.save_config.assert_not_called()
    assert path.read_bytes() == before


@pytest.mark.parametrize("online", [False, True])
@pytest.mark.parametrize("key", ["quantity", "ratio"])
def test_explicit_zero_remains_a_valid_numeric_value(numeric_service, online, key):
    service, public_id, path = numeric_service
    service.engine.is_available = lambda: online
    detail = service.detail(public_id)
    detail["config"]["numbers"][key] = 0
    assert service.save(public_id, detail["config"], detail["revision"])["saved"] is True
    assert json.loads(path.read_text("utf-8"))["numbers"][key] == 0
    assert service.engine.save_config.call_count == int(online)


@pytest.mark.parametrize("online", [False, True])
def test_explicit_null_default_keeps_optional_numeric_contract(numeric_service, online):
    service, public_id, path = numeric_service
    service.engine.is_available = lambda: online
    detail = service.detail(public_id)
    detail["config"]["numbers"]["optional"] = None
    assert service.save(public_id, detail["config"], detail["revision"])["saved"] is True
    assert json.loads(path.read_text("utf-8"))["numbers"]["optional"] is None


@pytest.mark.parametrize("field_type", ["int", "float"])
def test_numeric_schema_without_default_rejects_null(field_type):
    with pytest.raises(PluginCenterError, match="数量.*不能为空"):
        _validate_config({"count": {"type": field_type, "description": "数量"}}, {"count": None})


@pytest.mark.parametrize("container", ["object", "list", "template_list"])
def test_nested_numeric_null_uses_existing_recursive_validation(container):
    field = {"type": "float", "description": "嵌套数字", "default": 1.0}
    if container == "object":
        schema, value = {"type": "object", "items": {"number": field}}, {"number": None}
    elif container == "list":
        schema, value = {"type": "list", "items": field}, [None]
    else:
        schema = {"type": "template_list", "templates": {"test": {"items": {"number": field}}}}
        value = [{"__template_key": "test", "number": None}]
    with pytest.raises(PluginCenterError, match="嵌套数字.*不能为空"):
        _validate_config({"values": schema}, {"values": value})


def test_customer_numeric_nullability_matches_authoritative_validation():
    fields = {
        "quantity": {"type": "int", "default": 3},
        "ratio": {"type": "float"},
        "optional": {"type": "int", "default": None},
    }
    projected = _customer_schema(fields, {})
    assert projected["quantity"]["_nullable"] is False
    assert projected["ratio"]["_nullable"] is False
    assert projected["optional"]["_nullable"] is True
    assert "_nullable" not in fields["quantity"]
