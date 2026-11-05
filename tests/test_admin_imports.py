"""The service image must import using only the files declared by Docker COPY."""
from pathlib import Path
import shlex
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def test_admin_imports_from_minimal_docker_copy_files(tmp_path):
    stage = tmp_path / "image"
    stage.mkdir()
    dockerfile = (ROOT / "deploy/activation-service/Dockerfile").read_text(encoding="utf-8")
    for line in dockerfile.splitlines():
        if not line.startswith("COPY "):
            continue
        sources = shlex.split(line)[1:]
        destination = stage / sources.pop()
        for pattern in sources:
            matches = list(ROOT.glob(pattern))
            assert matches, f"Docker COPY input is missing: {pattern}"
            for source in matches:
                assert source.is_file(), f"Unexpected directory COPY: {pattern}"
                target = destination / source.name if line.endswith("/") else destination
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)

    code = r'''
from pathlib import Path
import sys
import sysconfig

stage = Path(sys.argv[1]).resolve()
# -I -S prevents repository paths, PYTHONPATH and editable-install .pth files
# from masking omissions. Only standard library and installed dependencies
# remain available beside the image's declared source files.
sys.path[:0] = [str(stage), str(stage / "src")]
for name in ("purelib", "platlib"):
    path = sysconfig.get_path(name)
    if path not in sys.path:
        sys.path.append(path)

from services.activation_service import cli
from services.activation_service.admin import app, auth, github
import qrcode

client = github.GitHubReleases(stage / "data")
assert client.repo and client.channel.public_key
for name, module in tuple(sys.modules.items()):
    if name.startswith(("wechatrebot_manager", "services.activation_service")):
        path = getattr(module, "__file__", None)
        if path:
            assert Path(path).resolve().is_relative_to(stage), (name, path)
assert "wechatrebot_manager.customer_logs" in sys.modules
assert "wechatrebot_manager.json_io" in sys.modules
print("minimal-admin-imports-ok")
'''
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-c", code, str(stage)],
        cwd=stage, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "minimal-admin-imports-ok"
