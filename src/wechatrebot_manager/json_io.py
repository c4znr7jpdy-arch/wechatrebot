from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any


def write_json_atomic(path: Path, payload: Any) -> None:
    """Write JSON without exposing readers to a partially written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        # Windows indexers and scanners can briefly deny replacement even
        # after our own handles are closed. Keep each attempt atomic and
        # bounded; never fall back to truncating the destination in place.
        for attempt in range(5):
            try:
                os.replace(temporary_name, path)
                break
            except OSError as exc:
                if getattr(exc, "winerror", None) not in {5, 32, 33} or attempt == 4:
                    raise
                time.sleep(0.05 * 2 ** attempt)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise
