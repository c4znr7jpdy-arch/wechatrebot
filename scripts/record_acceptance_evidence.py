from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re


_FORBIDDEN_CONTENT = (
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"(?i)\b(?:certificate[_ -]?password|pfx[_ -]?password)\b\s*[:=]"),
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _record(
    index_path: Path,
    evidence_path: Path,
    *,
    identifier: str,
    reviewer: str,
    approved_at: str,
) -> dict[str, object]:
    repository = index_path.resolve().parents[1]
    evidence = evidence_path.resolve()
    try:
        relative = evidence.relative_to(repository)
    except ValueError as exc:
        raise RuntimeError("evidence must stay inside the repository") from exc
    if "template" in evidence.name.casefold() or evidence.name.casefold() == "readme.md":
        raise RuntimeError("an evidence template cannot be approved")
    if not evidence.is_file() or evidence.stat().st_size == 0:
        raise RuntimeError("evidence must be a non-empty file")
    if evidence.stat().st_size <= 16 * 1024 * 1024:
        content = evidence.read_text(encoding="utf-8", errors="ignore")
        if any(pattern.search(content) for pattern in _FORBIDDEN_CONTENT):
            raise RuntimeError("evidence appears to contain prohibited secret material")

    index = json.loads(index_path.read_text(encoding="utf-8"))
    matching = [
        item
        for item in index.get("requirements", [])
        if isinstance(item, dict) and item.get("id") == identifier
    ]
    if len(matching) != 1:
        raise RuntimeError(f"acceptance evidence id is not unique: {identifier}")
    item = matching[0]
    item["status"] = "approved"
    item["reviewer"] = reviewer.strip()
    item["approved_at"] = approved_at
    item["evidence"] = [
        {"path": relative.as_posix(), "sha256": _sha256(evidence)}
    ]
    statuses = {
        entry.get("status")
        for entry in index["requirements"]
        if isinstance(entry, dict)
    }
    index["status"] = "approved" if statuses == {"approved"} else "pending"
    index_path.write_text(
        json.dumps(index, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return item


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Attach one accountable, hashed artifact to an acceptance check."
    )
    parser.add_argument("identifier")
    parser.add_argument("evidence", type=Path)
    parser.add_argument("--reviewer", required=True)
    parser.add_argument(
        "--index",
        type=Path,
        default=Path(__file__).resolve().parents[1]
        / "release"
        / "acceptance-evidence.json",
    )
    args = parser.parse_args(argv)
    if not args.reviewer.strip():
        parser.error("reviewer must not be empty")
    item = _record(
        args.index.resolve(),
        args.evidence,
        identifier=args.identifier,
        reviewer=args.reviewer,
        approved_at=datetime.now(timezone.utc).isoformat(),
    )
    print(json.dumps({"ok": True, "record": item}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
