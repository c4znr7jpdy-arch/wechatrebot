"""Source-only manager verification; no packaging or installation."""
from __future__ import annotations

import argparse
import hashlib
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.parse import urlsplit
import webbrowser

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src"
WATCH_ROOT = SOURCE / "wechatrebot_manager"
WEB_ROOT = WATCH_ROOT / "app_webui"
WATCH_SUFFIXES = {".py", ".html", ".css", ".js", ".json", ".woff2", ".png", ".ico"}


def snapshot(root: Path) -> tuple[tuple[str, int, int], ...]:
    items = []
    for path in root.rglob("*"):
        if "__pycache__" in path.parts or path.suffix not in WATCH_SUFFIXES:
            continue
        try:
            stat = path.stat()
            if path.is_file():
                items.append((path.relative_to(root).as_posix(), stat.st_mtime_ns, stat.st_size))
        except FileNotFoundError:
            continue  # An editor may atomically replace a file while scanning.
    return tuple(sorted(items))


def revision(root: Path) -> str:
    return hashlib.sha256(repr(snapshot(root)).encode()).hexdigest()


def preview_handler(root: Path, *, watch: bool = True):
    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(root), **kwargs)

        def log_message(self, *_args):
            pass

        def end_headers(self):
            self.send_header("Cache-Control", "no-store")
            super().end_headers()

        def do_GET(self):
            route = urlsplit(self.path).path
            if route == "/__dev_revision":
                self._send(revision(root).encode(), "text/plain")
                return
            target = Path(self.translate_path(self.path)).resolve()
            if not target.is_relative_to(root.resolve()):
                self.send_error(403)
                return
            if route in {"/", "/index.html"}:
                html = (root / "index.html").read_text(encoding="utf-8")
                badge = '<div style="position:fixed;bottom:4px;right:8px;z-index:99999;background:#fff3cd;color:#664d03;padding:4px 10px;font:12px sans-serif;pointer-events:none">源码预览 · 示例数据</div>'
                reload_script = ""
                if watch:
                    reload_script = '''<script>
const devRevision = %s;
setInterval(async () => {
  try {
    const response = await fetch('/__dev_revision', {cache: 'no-store'});
    if (response.ok && (await response.text()) !== devRevision) location.reload();
  } catch (_) {}
}, 1000);
</script>''' % json.dumps(revision(root))
                html = html.replace("</body>", badge + reload_script + "</body>")
                self._send(html.encode("utf-8"), "text/html; charset=utf-8")
                return
            if target.is_dir():
                self.send_error(404)
                return
            super().do_GET()

        def _send(self, body: bytes, content_type: str):
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


def resolve_install_root(explicit: Path | None) -> Path:
    from wechatrebot_manager.install_paths import INSTALL_MARKER
    from wechatrebot_manager.location import load_install_location

    root = explicit.expanduser().resolve() if explicit else load_install_location()
    if root is None:
        raise ValueError("未找到已安装环境。请传入 -InstallRoot，或使用 -Preview 仅验证界面。")
    try:
        marker = json.loads((root / INSTALL_MARKER).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"安装标记无效：{root}") from exc
    if not isinstance(marker, dict) or marker.get("state") not in {"ready", "configured"}:
        raise ValueError(f"安装尚未完成：{root}")
    return root


def stop_child(child):
    if child.poll() is None:
        child.terminate()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=5)


def supervise(command: list[str], watch_root: Path, *, watch: bool = True) -> int:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SOURCE) + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    before = snapshot(watch_root)
    child = subprocess.Popen(command, cwd=ROOT, env=env)
    try:
        while True:
            code = child.poll()
            if code in (0, 3) or (code is not None and not watch):
                return code
            time.sleep(0.5)
            if not watch:
                continue
            after = snapshot(watch_root)
            if after != before:
                # Debounce editor saves and multi-file changes.
                time.sleep(0.7)
                before = snapshot(watch_root)
                stop_child(child)
                print("源码已变化，正在重启验证窗口（机器人服务保持原状）。", flush=True)
                child = subprocess.Popen(command, cwd=ROOT, env=env)
    except KeyboardInterrupt:
        return 0
    finally:
        stop_child(child)


def run_child(root: Path) -> int:
    from wechatrebot_manager.app_shell import run_application
    from wechatrebot_manager.single_instance import SingleInstanceLock

    lock = SingleInstanceLock(root)
    if not lock.acquire():
        print("该安装目录的管理器已打开，请退出管理器窗口后重试；无需停止机器人。", flush=True)
        return 3
    try:
        return run_application(root, development=True)
    finally:
        lock.release()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preview", action="store_true", help="UI only, using example data")
    parser.add_argument("--install-root", type=Path)
    parser.add_argument("--check", action="store_true", help="preflight only; opens no windows")
    parser.add_argument("--no-watch", action="store_true")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    sys.path.insert(0, str(SOURCE))
    if args.preview and (args.install_root or args.child):
        parser.error("preview cannot be combined with an installed environment")
    root = None
    if not args.preview:
        try:
            root = resolve_install_root(args.install_root)
        except ValueError as exc:
            parser.error(str(exc))
        import webview  # noqa: F401 -- fail before entering the watch loop
        from wechatrebot_manager import app_shell
        if app_shell._asset_root().resolve() != WEB_ROOT.resolve():
            parser.error("manager assets are not loaded from this source checkout")
    if args.check:
        print(json.dumps({"ok": True, "mode": "preview" if args.preview else "source",
                          "source": str(SOURCE), "install_root": str(root) if root else None,
                          "python": sys.executable, "watch": not args.no_watch}, ensure_ascii=False))
        return 0
    if args.child:
        return run_child(root)
    if args.preview:
        with ThreadingHTTPServer(("127.0.0.1", 0), preview_handler(WEB_ROOT, watch=not args.no_watch)) as server:
            url = f"http://127.0.0.1:{server.server_port}/?preview"
            print(f"源码预览（示例数据）：{url}\n保存界面文件后自动刷新，Ctrl+C 结束。", flush=True)
            if not args.no_browser:
                webbrowser.open(url)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
        return 0
    print(f"源码：{SOURCE}\n实际安装目录：{root}\n界面操作会作用于该安装环境。保存源码后自动重启窗口，未保存的表单会丢失；-NoWatch 可关闭监听。", flush=True)
    command = [sys.executable, "-B", str(Path(__file__).resolve()), "--child", "--install-root", str(root)]
    return supervise(command, WATCH_ROOT, watch=not args.no_watch)


if __name__ == "__main__":
    raise SystemExit(main())
