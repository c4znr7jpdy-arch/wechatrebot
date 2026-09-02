from pathlib import Path


def test_smoke_install_script_never_starts_services() -> None:
    source = (
        Path(__file__).parents[1] / "scripts" / "smoke_install.py"
    ).read_text(encoding="utf-8")
    assert "run_services_foreground" not in source
    assert '"services_started": False' in source
