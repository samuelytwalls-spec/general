"""Enrolled remote devices and the command queue they poll.

Each device gets a random secret when it is enrolled. Two keys are derived
from it: one authenticates the device to this API, the other signs every
command envelope so the device can verify that a command really came from
this Pi, was meant for it, and has not been replayed or altered.

Devices connect out to the Pi (long-polling), so nothing has to listen for
inbound connections on the laptops/desktops that run the client.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
import time
import uuid

from .config import DevicesConfig

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    name TEXT PRIMARY KEY,
    secret TEXT NOT NULL,
    created REAL NOT NULL,
    last_seen REAL,
    info TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS commands (
    id TEXT PRIMARY KEY,
    device TEXT NOT NULL,
    command TEXT NOT NULL,
    timeout INTEGER NOT NULL,
    status TEXT NOT NULL,          -- queued, sent, done, rejected, error, expired
    created REAL NOT NULL,
    claimed REAL,
    finished REAL,
    exit_code INTEGER,
    stdout TEXT,
    stderr TEXT,
    reason TEXT,
    requested_by TEXT
);
CREATE INDEX IF NOT EXISTS commands_device_status ON commands(device, status);
"""

FINAL_STATES = ("done", "rejected", "error", "expired")


def canonical_json(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def derive_keys(secret: str) -> tuple[str, bytes]:
    """Return (auth_token, signing_key) for a device secret."""
    key = secret.encode()
    auth = hmac.new(key, b"piagent-device-auth-v1", hashlib.sha256).hexdigest()
    sign = hmac.new(key, b"piagent-command-sign-v1", hashlib.sha256).digest()
    return auth, sign


def sign_envelope(secret: str, envelope: dict) -> str:
    return hmac.new(derive_keys(secret)[1], canonical_json(envelope), hashlib.sha256).hexdigest()


class DeviceError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class DeviceHub:
    def __init__(self, cfg: DevicesConfig):
        self.cfg = cfg
        self._cond = threading.Condition()
        os.makedirs(os.path.dirname(cfg.db_path) or ".", exist_ok=True)
        with self._db() as conn:
            conn.executescript(SCHEMA)

    @contextlib.contextmanager
    def _db(self):
        """Connection that commits on success, rolls back on error, then closes."""
        conn = sqlite3.connect(self.cfg.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            with conn:
                yield conn
        finally:
            conn.close()

    # -- enrollment ------------------------------------------------------
    def add(self, name: str) -> str:
        if not NAME_RE.match(name or ""):
            raise DeviceError("device name must be 1-32 chars of a-z, 0-9, '-' or '_'")
        secret = secrets.token_hex(32)
        with self._db() as conn:
            try:
                conn.execute("INSERT INTO devices(name, secret, created) VALUES (?, ?, ?)", (name, secret, time.time()))
            except sqlite3.IntegrityError:
                raise DeviceError(f"device {name!r} already exists", 409) from None
        return secret

    def remove(self, name: str) -> bool:
        with self._db() as conn:
            removed = conn.execute("DELETE FROM devices WHERE name = ?", (name,)).rowcount > 0
            conn.execute(
                "UPDATE commands SET status = 'expired', reason = 'device removed', finished = ? "
                "WHERE device = ? AND status IN ('queued', 'sent')",
                (time.time(), name),
            )
        with self._cond:
            self._cond.notify_all()
        return removed

    def list(self) -> list[dict]:
        now = time.time()
        with self._db() as conn:
            rows = conn.execute("SELECT name, created, last_seen, info FROM devices ORDER BY name").fetchall()
            pending = dict(conn.execute(
                "SELECT device, COUNT(*) FROM commands WHERE status IN ('queued','sent') GROUP BY device"
            ).fetchall())
        out = []
        for r in rows:
            info = json.loads(r["info"] or "{}")
            out.append({
                "name": r["name"],
                "online": bool(r["last_seen"] and now - r["last_seen"] < self.cfg.online_window),
                "last_seen": r["last_seen"],
                "created": r["created"],
                "platform": info.get("platform"),
                "hostname": info.get("hostname"),
                "client_version": info.get("version"),
                "policy": info.get("policy"),
                "pending_commands": pending.get(r["name"], 0),
            })
        return out

    def exists(self, name: str) -> bool:
        with self._db() as conn:
            return conn.execute("SELECT 1 FROM devices WHERE name = ?", (name,)).fetchone() is not None

    # -- device side -----------------------------------------------------
    def authenticate(self, name: str, token: str) -> bool:
        if not name or not token:
            return False
        with self._db() as conn:
            row = conn.execute("SELECT secret FROM devices WHERE name = ?", (name,)).fetchone()
        if not row:
            hmac.compare_digest("x" * 64, token)  # keep timing similar for unknown names
            return False
        return hmac.compare_digest(derive_keys(row["secret"])[0], token)

    def touch(self, name: str, info: dict | None = None) -> None:
        with self._db() as conn:
            if info is not None:
                limits = {"hostname": 200, "platform": 100, "version": 50, "policy": 1000}
                keep = {k: str(v)[:limits[k]] for k, v in info.items() if k in limits}
                conn.execute("UPDATE devices SET last_seen = ?, info = ? WHERE name = ?", (time.time(), json.dumps(keep), name))
            else:
                conn.execute("UPDATE devices SET last_seen = ? WHERE name = ?", (time.time(), name))

    def _expire_old(self, conn: sqlite3.Connection) -> None:
        now = time.time()
        conn.execute(
            "UPDATE commands SET status = 'expired', reason = 'not picked up in time', finished = ? "
            "WHERE status = 'queued' AND created < ?",
            (now, now - self.cfg.command_ttl),
        )
        conn.execute(
            "UPDATE commands SET status = 'expired', reason = 'no result reported', finished = ? "
            "WHERE status = 'sent' AND claimed < ? - timeout - 60",
            (now, now),
        )

    def claim_next(self, name: str, wait: float) -> dict | None:
        """Return the next signed command for ``name``, waiting up to ``wait`` seconds."""
        deadline = time.monotonic() + max(0.0, min(wait, self.cfg.poll_wait_max))
        while True:
            with self._db() as conn:
                self._expire_old(conn)
                row = conn.execute(
                    "SELECT c.id, c.command, c.timeout, c.created, d.secret FROM commands c "
                    "JOIN devices d ON d.name = c.device "
                    "WHERE c.device = ? AND c.status = 'queued' ORDER BY c.created LIMIT 1",
                    (name,),
                ).fetchone()
                if row:
                    now = time.time()
                    conn.execute("UPDATE commands SET status = 'sent', claimed = ? WHERE id = ?", (now, row["id"]))
                    envelope = {
                        "v": 1,
                        "id": row["id"],
                        "device": name,
                        "command": row["command"],
                        "timeout": row["timeout"],
                        "issued_at": int(now),
                        "expires_at": int(now + self.cfg.command_ttl),
                        "nonce": secrets.token_hex(16),
                    }
                    return {"envelope": envelope, "signature": sign_envelope(row["secret"], envelope)}
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            with self._cond:
                self._cond.wait(timeout=min(remaining, 5.0))

    def complete(self, name: str, command_id: str, result: dict) -> None:
        status = result.get("status", "done")
        if status not in ("done", "rejected", "error"):
            raise DeviceError("status must be done, rejected or error")
        with self._db() as conn:
            row = conn.execute("SELECT status FROM commands WHERE id = ? AND device = ?", (command_id, name)).fetchone()
            if not row:
                raise DeviceError("unknown command id", 404)
            if row["status"] != "sent":
                raise DeviceError(f"command is {row['status']}, not awaiting a result", 409)
            exit_code = result.get("exit_code")
            conn.execute(
                "UPDATE commands SET status = ?, finished = ?, exit_code = ?, stdout = ?, stderr = ?, reason = ? WHERE id = ?",
                (
                    status,
                    time.time(),
                    int(exit_code) if isinstance(exit_code, int) else None,
                    str(result.get("stdout", ""))[:65536],
                    str(result.get("stderr", ""))[:65536],
                    str(result.get("reason", ""))[:1000],
                    command_id,
                ),
            )
        with self._cond:
            self._cond.notify_all()

    # -- Pi side -----------------------------------------------------------
    def enqueue(self, name: str, command: str, timeout: int = 60, requested_by: str = "api") -> str:
        if not self.exists(name):
            raise DeviceError(f"unknown device {name!r}", 404)
        if not command.strip() or len(command) > 4096:
            raise DeviceError("command must be 1-4096 characters")
        cid = uuid.uuid4().hex
        with self._db() as conn:
            conn.execute(
                "INSERT INTO commands(id, device, command, timeout, status, created, requested_by) "
                "VALUES (?, ?, ?, ?, 'queued', ?, ?)",
                (cid, name, command, max(1, min(int(timeout), 3600)), time.time(), requested_by),
            )
        with self._cond:
            self._cond.notify_all()
        return cid

    def get(self, command_id: str) -> dict | None:
        with self._db() as conn:
            row = conn.execute("SELECT * FROM commands WHERE id = ?", (command_id,)).fetchone()
        return dict(row) if row else None

    def wait_result(self, command_id: str, timeout: float) -> dict | None:
        deadline = time.monotonic() + timeout
        while True:
            cmd = self.get(command_id)
            if cmd is None or cmd["status"] in FINAL_STATES:
                return cmd
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return cmd
            with self._cond:
                self._cond.wait(timeout=min(remaining, 2.0))
