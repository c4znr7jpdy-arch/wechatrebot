from pathlib import Path
import sys

# The standalone service imports the same protocol as the manager without
# requiring Windows GUI dependencies on its server host.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

from .cli import main  # noqa: E402

raise SystemExit(main())
