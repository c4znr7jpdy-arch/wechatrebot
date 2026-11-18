import copy
import json
from pathlib import Path

import pytest

from benchmark_manager_bootstrap import check_budget


@pytest.fixture
def budget():
    return json.loads((Path(__file__).parent / "fixtures/manager_bootstrap_budget.json").read_text("utf-8"))


def test_budget_accepts_lower_work_and_does_not_gate_wall_time(budget):
    report = copy.deepcopy(budget)
    report["reads_per_bootstrap"]["plugin_config"] = 0
    report["wall_ms"] = {"median": 100000}
    assert check_budget(report, budget) == []


@pytest.mark.parametrize("category,name", [
    ("reads_per_bootstrap", "plugin_config"), ("probes_per_bootstrap", "lifecycle_refresh"),
    ("reads_per_bootstrap", "unexpected_scan"),
])
def test_budget_detects_extra_work(budget, category, name):
    report = copy.deepcopy(budget)
    report[category][name] = report[category].get(name, 0) + 1
    assert check_budget(report, budget) == [f"{category}.{name}: {report[category][name]} > {budget[category].get(name, 0)}"]


@pytest.mark.parametrize("field,value", [("fixture", {"providers": 1, "plugins": 40}),
                                        ("schema_version", 2), ("reads_per_bootstrap", {"engine_config": -1})])
def test_budget_rejects_mismatched_or_invalid_limits(budget, field, value):
    invalid = budget | {field: value}
    with pytest.raises(ValueError):
        check_budget(copy.deepcopy(budget), invalid)
