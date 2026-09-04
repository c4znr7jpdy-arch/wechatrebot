import importlib.util
import json
from pathlib import Path


def _recorder():
    script = Path(__file__).parents[1] / "scripts" / "record_acceptance_evidence.py"
    spec = importlib.util.spec_from_file_location("record_acceptance_evidence", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_recorded_approval_contains_reviewer_time_path_and_hash(tmp_path: Path) -> None:
    release = tmp_path / "release"
    evidence_dir = release / "evidence"
    evidence_dir.mkdir(parents=True)
    index_path = release / "acceptance-evidence.json"
    index_path.write_text(
        json.dumps(
            {
                "status": "pending",
                "requirements": [
                    {
                        "id": "hook-security",
                        "status": "pending",
                        "reviewer": "",
                        "approved_at": "",
                        "evidence": [],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    evidence = evidence_dir / "0.8.3-hook-security.md"
    evidence.write_text("reviewed exact hashes\n", encoding="utf-8")

    result = _recorder()._record(
        index_path,
        evidence,
        identifier="hook-security",
        reviewer="Security Reviewer",
        approved_at="2026-09-03T18:00:00+08:00",
    )

    assert result["status"] == "approved"
    assert result["reviewer"] == "Security Reviewer"
    assert result["evidence"][0]["path"] == "release/evidence/0.8.3-hook-security.md"
    assert len(result["evidence"][0]["sha256"]) == 64
