"""Local credentials and bounded server-side authentication state."""
from __future__ import annotations

import base64
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import struct
import urllib.parse

from wechatrebot_manager.licensing.protocol import LicenseError

AUTH_FILE = "admin-auth.json"
TOKEN_FILE = "admin-github.json"
SESSION_FILE = "admin-state.sqlite3"
PREFIX = "/wechatrebot-admin/"
COOKIE = "wrb_admin"
ITERATIONS = 600_000
SESSION_SECONDS = 12 * 3600


@dataclass(frozen=True)
class Authentication:
    status: str
    revision: str = ""


def private_json(path: Path, value: dict, *, replace: bool = False) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    encoded = (json.dumps(value, ensure_ascii=False) + "\n").encode("utf-8")
    temporary = path.with_name("." + path.name + "." + secrets.token_hex(8))
    target = temporary if replace else path
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        if replace:
            os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if replace:
            temporary.unlink(missing_ok=True)


def read_private_json(path: Path, *, maximum: int = 16 * 1024) -> dict:
    if path.is_symlink():
        raise ValueError("Credential links are not supported")
    with path.open("rb") as handle:
        raw = handle.read(maximum + 1)
    if len(raw) > maximum:
        raise ValueError("Credential file is too large")
    result = json.loads(raw)
    if not isinstance(result, dict):
        raise ValueError("Invalid credentials")
    return result


def initialize(data_dir: Path, password: str, *, reset: bool = False) -> dict:
    if not 16 <= len(password) <= 1024:
        raise LicenseError("invalid_password", "管理密码须为 16 至 1024 个字符。")
    salt = secrets.token_bytes(32)
    secret = base64.b32encode(secrets.token_bytes(20)).decode("ascii")
    value = {"schema_version": 1, "revision": secrets.token_hex(32), "iterations": ITERATIONS,
             "salt": salt.hex(), "password_hash": hashlib.pbkdf2_hmac(
                 "sha256", password.encode("utf-8"), salt, ITERATIONS).hex(), "totp_secret": secret}
    private_json(data_dir / AUTH_FILE, value, replace=reset)
    label = urllib.parse.quote("WeChatReBot:maintainer", safe="")
    uri = f"otpauth://totp/{label}?secret={secret}&issuer=WeChatReBot&algorithm=SHA1&digits=6&period=30"
    return {"secret": secret, "provisioning_uri": uri}


def set_github_token(data_dir: Path, token: str) -> None:
    token = token.strip()
    if not re.fullmatch(r"github_pat_[A-Za-z0-9_]{20,250}", token):
        raise LicenseError("invalid_token", "请输入仅授权本仓库 Contents 读写的细粒度 GitHub 令牌。")
    private_json(data_dir / TOKEN_FILE, {"schema_version": 1, "token": token}, replace=True)


def totp(secret: str, counter: int) -> str:
    digest = hmac.new(base64.b32decode(secret), struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 15
    number = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7fffffff
    return f"{number % 1_000_000:06d}"


def totp_counter(secret: str, code: str, now: int) -> int | None:
    if not re.fullmatch(r"[0-9]{6}", code):
        return None
    for counter in (now // 30, now // 30 - 1, now // 30 + 1):
        if counter >= 0 and hmac.compare_digest(totp(secret, counter), code):
            return counter
    return None


class AuthStore:
    def __init__(self, data_dir: Path):
        self.directory = data_dir
        self.path = data_dir / SESSION_FILE
        data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS sessions (
                    digest TEXT PRIMARY KEY, csrf TEXT NOT NULL, expires INTEGER NOT NULL,
                    revision TEXT NOT NULL, authenticated INTEGER NOT NULL DEFAULT 0,
                    flash TEXT NOT NULL DEFAULT '');
                CREATE TABLE IF NOT EXISTS attempts (
                    revision TEXT PRIMARY KEY, failures INTEGER NOT NULL DEFAULT 0,
                    locked_until INTEGER NOT NULL DEFAULT 0, last_counter INTEGER NOT NULL DEFAULT -1);
            """)
        os.chmod(self.path, 0o600)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA secure_delete=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def credentials(self) -> dict | None:
        try:
            value = read_private_json(self.directory / AUTH_FILE)
        except FileNotFoundError:
            return None
        if (value.get("schema_version") != 1 or value.get("iterations") != ITERATIONS
                or not re.fullmatch(r"[0-9a-f]{64}", str(value.get("revision", "")))
                or not re.fullmatch(r"[0-9a-f]{64}", str(value.get("salt", "")))
                or not re.fullmatch(r"[0-9a-f]{64}", str(value.get("password_hash", "")))
                or not re.fullmatch(r"[A-Z2-7]{32}", str(value.get("totp_secret", "")))):
            raise ValueError("Invalid administrator credentials")
        return value

    @staticmethod
    def digest(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def new_session(self, now: int, *, authenticated: bool = False, revision: str = "") -> tuple[str, dict]:
        credentials = self.credentials()
        if credentials is None:
            raise ValueError("Administrator is not initialized")
        if authenticated and (not revision or revision != credentials["revision"]):
            raise LicenseError("login_reset", "管理员凭据已重置，请重新登录。")
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        expires = now + (SESSION_SECONDS if authenticated else 600)
        with self.connection() as db:
            db.execute("DELETE FROM sessions WHERE expires<=?", (now,))
            db.execute("INSERT INTO sessions(digest,csrf,expires,revision,authenticated) VALUES(?,?,?,?,?)",
                       (self.digest(token), csrf, expires, credentials["revision"], int(authenticated)))
        return token, {"csrf": csrf, "expires": expires, "authenticated": authenticated}

    def session(self, token: str, now: int) -> dict | None:
        if not token or len(token) > 128:
            return None
        credentials = self.credentials()
        if credentials is None:
            return None
        with self.connection() as db:
            row = db.execute("SELECT * FROM sessions WHERE digest=? AND expires>? AND revision=?",
                             (self.digest(token), now, credentials["revision"])).fetchone()
        return dict(row) if row else None

    def logout(self, token: str) -> None:
        with self.connection() as db:
            db.execute("DELETE FROM sessions WHERE digest=?", (self.digest(token),))

    def authenticate(self, password: str, code: str, now: int, *, step_up: bool = False) -> Authentication:
        """Return ok/invalid/locked; one global lockout also covers step-up attempts."""
        credentials = self.credentials()
        if credentials is None:
            return Authentication("invalid")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT OR IGNORE INTO attempts(revision) VALUES(?)", (credentials["revision"],))
            row = db.execute("SELECT * FROM attempts WHERE revision=?", (credentials["revision"],)).fetchone()
            if row["locked_until"] > now:
                return Authentication("locked")
            password_ok = step_up
            if not step_up and len(password) <= 1024:
                digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                            bytes.fromhex(credentials["salt"]), ITERATIONS).hex()
                password_ok = hmac.compare_digest(credentials["password_hash"], digest)
            counter = totp_counter(credentials["totp_secret"], code, now)
            if password_ok and counter is not None and counter > row["last_counter"]:
                db.execute("UPDATE attempts SET failures=0,locked_until=0,last_counter=? WHERE revision=?",
                           (counter, credentials["revision"]))
                return Authentication("ok", credentials["revision"])
            failures = (0 if row["locked_until"] else row["failures"]) + 1
            locked = now + 900 if failures >= 5 else 0
            db.execute("UPDATE attempts SET failures=?,locked_until=? WHERE revision=?",
                       (failures, locked, credentials["revision"]))
            return Authentication("locked" if locked else "invalid")

    def locked_seconds(self, now: int) -> int:
        credentials = self.credentials()
        if credentials is None:
            return 0
        with self.connection() as db:
            row = db.execute("SELECT locked_until FROM attempts WHERE revision=?", (credentials["revision"],)).fetchone()
        return max(0, row[0] - now) if row else 0

    def flash(self, token: str, value: str | None = None) -> str:
        """Consume once after a redirect; the activation code is never in a URL/cookie."""
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT flash FROM sessions WHERE digest=?", (self.digest(token),)).fetchone()
            db.execute("UPDATE sessions SET flash=? WHERE digest=?", (value or "", self.digest(token)))
        return row[0] if row and value is None else ""
