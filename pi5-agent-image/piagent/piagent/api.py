"""Authenticated, LAN-only HTTP(S) API.

Admin endpoints need ``Authorization: Bearer <token>`` where the token is the
contents of /etc/piagent/token. Device endpoints (used by the companion
client) authenticate with a per-device key instead. Requests from addresses
outside ``api.allowed_networks`` are refused before anything else happens;
the nftables firewall in the image enforces the same rule at the packet level.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import os
import re
import socket
import ssl
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from . import __version__, safety
from .agent import Agent, Session
from .config import Config
from .devices import DeviceError, DeviceHub
from .llm import LlamaClient
from .rag import DocIndex

MAX_MESSAGE_CHARS = 8000


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def cert_fingerprint(cert_path: str) -> str | None:
    try:
        with open(cert_path) as fh:
            der = ssl.PEM_cert_to_DER_cert(fh.read())
    except (OSError, ValueError):
        return None
    digest = hashlib.sha256(der).hexdigest().upper()
    return ":".join(digest[i:i + 2] for i in range(0, len(digest), 2))


class ApiError(Exception):
    def __init__(self, status: int, message: str, **extra):
        super().__init__(message)
        self.status = status
        self.payload = {"error": message, **extra}


class AppState:
    def __init__(self, cfg: Config, llm: LlamaClient | None = None, docs=None, hub=None, agent=None):
        self.cfg = cfg
        self.llm = llm or LlamaClient(cfg.llm)
        self.docs = docs if docs is not None else DocIndex(cfg.rag, self.llm)
        self.hub = hub if hub is not None else DeviceHub(cfg.devices)
        self.agent = agent or Agent(cfg, self.llm, self.docs, self.hub)
        self.networks = [ipaddress.ip_network(n, strict=False) for n in cfg.api.allowed_networks]
        self.sessions: dict[str, Session] = {}
        self.sessions_lock = threading.Lock()
        self.index_lock = threading.Lock()
        self._token: tuple[tuple, str | None] = ((), None)
        self._failures: dict[str, list[float]] = {}
        self._failures_lock = threading.Lock()
        self.fingerprint = cert_fingerprint(cfg.api.tls_cert) if cfg.api.tls else None

    # -- access control ------------------------------------------------------
    def is_lan(self, host: str) -> bool:
        try:
            ip = ipaddress.ip_address(host.split("%", 1)[0])
        except ValueError:
            return False
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        return any(ip in net for net in self.networks if net.version == ip.version)

    def admin_token(self) -> str | None:
        path = self.cfg.api.token_file
        try:
            st = os.stat(path)
        except OSError:
            return None
        key = (st.st_mtime_ns, st.st_size, st.st_ino)
        if self._token[0] != key:
            with open(path) as fh:
                token = fh.read().strip()
            self._token = (key, token if len(token) >= 32 else None)
        return self._token[1]

    def throttled(self, ip: str) -> bool:
        now = time.time()
        with self._failures_lock:
            recent = [t for t in self._failures.get(ip, []) if now - t < 300]
            self._failures[ip] = recent
            return len(recent) >= 10

    def record_failure(self, ip: str) -> None:
        with self._failures_lock:
            self._failures.setdefault(ip, []).append(time.time())

    # -- sessions --------------------------------------------------------------
    def session(self, session_id: str | None, create: bool = False) -> Session:
        now = time.time()
        with self.sessions_lock:
            for sid in [s for s, v in self.sessions.items() if now - v.updated > self.cfg.api.session_ttl]:
                del self.sessions[sid]
            if session_id and session_id in self.sessions:
                return self.sessions[session_id]
            if session_id and not create:
                raise ApiError(404, "unknown or expired session")
            sess = Session()
            self.sessions[sess.id] = sess
            return sess

    def public_url(self) -> str:
        if self.cfg.api.public_url:
            return self.cfg.api.public_url
        scheme = "https" if self.cfg.api.tls else "http"
        return f"{scheme}://{socket.gethostname()}.local:{self.cfg.api.port}"


# -- route handlers ------------------------------------------------------------
def _str(body: dict, key: str, required: bool = True, max_len: int = 4096) -> str:
    value = body.get(key)
    if value is None and not required:
        return ""
    if not isinstance(value, str) or (required and not value.strip()) or len(value) > max_len:
        raise ApiError(400, f"'{key}' must be a non-empty string of at most {max_len} characters")
    return value


def h_ping(st: AppState, h, m, body):
    return 200, {"ok": True}


def h_status(st: AppState, h, m, body):
    try:
        docs = st.docs.stats()
    except Exception as exc:  # noqa: BLE001 - report, don't fail the status call
        docs = {"error": str(exc)}
    return 200, {
        "version": __version__,
        "chat_model_ready": st.llm.healthy("chat"),
        "embed_model_ready": st.llm.healthy("embed"),
        "docs": docs,
        "devices": len(st.hub.list()),
        "sessions": len(st.sessions),
        "tls_fingerprint": st.fingerprint,
    }


def _locked(sess: Session):
    if not sess.lock.acquire(blocking=False):
        raise ApiError(409, "this session is busy with another request")
    return sess.lock


def h_chat(st: AppState, h, m, body):
    message = _str(body, "message", max_len=MAX_MESSAGE_CHARS)
    sess = st.session(body.get("session_id"), create=False) if body.get("session_id") else st.session(None, create=True)
    lock = _locked(sess)
    try:
        if sess.pending:
            raise ApiError(409, "session is waiting for a confirmation", pending=sess.summary()["pending"])
        return 200, st.agent.run_turn(sess, message)
    finally:
        lock.release()


def h_confirm(st: AppState, h, m, body):
    sess = st.session(m.group("sid"))
    approve = body.get("approve")
    if not isinstance(approve, bool):
        raise ApiError(400, "'approve' must be true or false")
    lock = _locked(sess)
    try:
        if not sess.pending:
            raise ApiError(409, "nothing is waiting for confirmation")
        if body.get("pending_id") and body["pending_id"] != sess.pending.id:
            raise ApiError(409, "pending_id does not match the action awaiting confirmation")
        return 200, st.agent.resolve(sess, approve)
    finally:
        lock.release()


def h_session_get(st: AppState, h, m, body):
    return 200, st.session(m.group("sid")).summary()


def h_session_delete(st: AppState, h, m, body):
    with st.sessions_lock:
        removed = st.sessions.pop(m.group("sid"), None)
    if not removed:
        raise ApiError(404, "unknown session")
    return 200, {"deleted": True}


def h_search(st: AppState, h, m, body):
    query = _str(body, "query", max_len=2000)
    k = body.get("k", st.cfg.rag.top_k)
    if not isinstance(k, int) or not 1 <= k <= 20:
        raise ApiError(400, "'k' must be an integer between 1 and 20")
    return 200, {"results": st.docs.search(query, k)}


def h_index(st: AppState, h, m, body):
    if not st.index_lock.acquire(blocking=False):
        raise ApiError(409, "indexing is already running")
    try:
        return 200, st.docs.index(full=bool(body.get("full")))
    finally:
        st.index_lock.release()


def h_index_stats(st: AppState, h, m, body):
    return 200, st.docs.stats()


def h_devices(st: AppState, h, m, body):
    return 200, {"devices": st.hub.list()}


def h_device_add(st: AppState, h, m, body):
    name = _str(body, "name", max_len=32)
    secret = st.hub.add(name)
    return 201, {
        "name": name,
        "client_config": {
            "server": st.public_url(),
            "device": name,
            "secret": secret,
            "tls_fingerprint": st.fingerprint,
        },
        "note": "Store client_config on the device; the secret is not shown again.",
    }


def h_device_delete(st: AppState, h, m, body):
    if not st.hub.remove(m.group("name")):
        raise ApiError(404, "unknown device")
    return 200, {"deleted": True}


def h_device_command(st: AppState, h, m, body):
    name = m.group("name")
    command = _str(body, "command")
    timeout = body.get("timeout", 60)
    if not isinstance(timeout, int) or not 1 <= timeout <= 3600:
        raise ApiError(400, "'timeout' must be an integer between 1 and 3600")
    verdict = safety.classify(command, st.cfg.agent.extra_confirm_patterns, st.cfg.agent.extra_deny_patterns)
    if verdict.level == safety.DENY:
        raise ApiError(403, "command refused by policy", reasons=verdict.reasons)
    if verdict.level == safety.CONFIRM and body.get("confirm") is not True:
        raise ApiError(409, "confirmation_required", reasons=verdict.reasons,
                       hint="resend with \"confirm\": true to run this command")
    cid = st.hub.enqueue(name, command, timeout, requested_by="api")
    wait = body.get("wait", 0)
    if isinstance(wait, (int, float)) and wait > 0:
        return 200, st.hub.wait_result(cid, min(float(wait), st.cfg.devices.result_timeout))
    return 202, {"id": cid, "status": "queued"}


def h_device_command_get(st: AppState, h, m, body):
    cmd = st.hub.get(m.group("cid"))
    if not cmd or cmd["device"] != m.group("name"):
        raise ApiError(404, "unknown command")
    return 200, cmd


def h_agent_hello(st: AppState, h, m, body):
    st.hub.touch(h.device, {k: body.get(k, "") for k in ("hostname", "platform", "version", "policy")})
    return 200, {"ok": True, "server_time": time.time(), "poll_wait_max": st.cfg.devices.poll_wait_max}


def h_agent_poll(st: AppState, h, m, body):
    wait = body.get("wait", st.cfg.devices.poll_wait_max)
    if not isinstance(wait, (int, float)):
        raise ApiError(400, "'wait' must be a number")
    st.hub.touch(h.device)
    item = st.hub.claim_next(h.device, float(wait))
    st.hub.touch(h.device)
    return (200, item) if item else (204, None)


def h_agent_result(st: AppState, h, m, body):
    st.hub.complete(h.device, _str(body, "id", max_len=64), body)
    return 200, {"ok": True}


ROUTES = [
    ("GET", r"/v1/ping", h_ping, "none"),
    ("GET", r"/v1/status", h_status, "admin"),
    ("POST", r"/v1/chat", h_chat, "admin"),
    ("POST", r"/v1/sessions/(?P<sid>[0-9a-f]{32})/confirm", h_confirm, "admin"),
    ("GET", r"/v1/sessions/(?P<sid>[0-9a-f]{32})", h_session_get, "admin"),
    ("DELETE", r"/v1/sessions/(?P<sid>[0-9a-f]{32})", h_session_delete, "admin"),
    ("POST", r"/v1/search", h_search, "admin"),
    ("POST", r"/v1/index", h_index, "admin"),
    ("GET", r"/v1/index", h_index_stats, "admin"),
    ("GET", r"/v1/devices", h_devices, "admin"),
    ("POST", r"/v1/devices", h_device_add, "admin"),
    ("DELETE", r"/v1/devices/(?P<name>[a-z0-9_-]{1,32})", h_device_delete, "admin"),
    ("POST", r"/v1/devices/(?P<name>[a-z0-9_-]{1,32})/commands", h_device_command, "admin"),
    ("GET", r"/v1/devices/(?P<name>[a-z0-9_-]{1,32})/commands/(?P<cid>[0-9a-f]{32})", h_device_command_get, "admin"),
    ("POST", r"/v1/agent/hello", h_agent_hello, "device"),
    ("POST", r"/v1/agent/poll", h_agent_poll, "device"),
    ("POST", r"/v1/agent/result", h_agent_result, "device"),
]
_COMPILED = [(meth, re.compile(f"^{pat}$"), fn, auth) for meth, pat, fn, auth in ROUTES]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "piagent"
    sys_version = ""
    state: AppState  # set on the subclass by make_server
    device: str = ""

    def log_message(self, fmt, *args):  # one line per request, never the headers
        log(f"{self.client_address[0]} {fmt % args}")

    def _send(self, status: int, payload) -> None:
        data = b"" if payload is None else json.dumps(payload, default=str).encode()
        self.send_response(status)
        if payload is not None:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if data:
            self.wfile.write(data)

    def _body(self) -> dict:
        length = self.headers.get("Content-Length")
        if not length:
            return {}
        try:
            n = int(length)
        except ValueError:
            raise ApiError(400, "bad Content-Length") from None
        if n < 0 or n > self.state.cfg.api.max_body_bytes:
            raise ApiError(413, "request body too large")
        raw = self.rfile.read(n)
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            raise ApiError(400, "body must be JSON") from None
        if not isinstance(body, dict):
            raise ApiError(400, "body must be a JSON object")
        return body

    def _bearer(self) -> str:
        value = self.headers.get("Authorization", "")
        return value[7:].strip() if value.lower().startswith("bearer ") else ""

    def _authorize(self, kind: str) -> None:
        ip = self.client_address[0]
        if kind == "none":
            return
        if self.state.throttled(ip):
            raise ApiError(429, "too many failed authentication attempts; try again later")
        if kind == "admin":
            expected = self.state.admin_token()
            if expected is None:
                raise ApiError(503, "API token is not configured (see /etc/piagent/token)")
            if hmac.compare_digest(self._bearer().encode(), expected.encode()):
                return
        elif kind == "device":
            name = self.headers.get("X-PiAgent-Device", "")
            if self.state.hub.authenticate(name, self._bearer()):
                self.device = name
                return
        self.state.record_failure(ip)
        raise ApiError(401, "unauthorized")

    def _dispatch(self, method: str) -> None:
        try:
            if not self.state.is_lan(self.client_address[0]):
                raise ApiError(403, "only clients on the local network may use this API")
            path = urlparse(self.path).path.rstrip("/") or "/"
            allowed = []
            for meth, regex, fn, auth in _COMPILED:
                match = regex.match(path)
                if not match:
                    continue
                if meth != method:
                    allowed.append(meth)
                    continue
                self._authorize(auth)
                body = self._body() if method == "POST" else {}
                status, payload = fn(self.state, self, match, body)
                self._send(status, payload)
                return
            raise ApiError(405 if allowed else 404, "method not allowed" if allowed else "not found")
        except ApiError as exc:
            self._send(exc.status, exc.payload)
        except DeviceError as exc:
            self._send(exc.status, {"error": str(exc)})
        except (BrokenPipeError, ConnectionResetError, ssl.SSLError, TimeoutError):
            self.close_connection = True
        except Exception as exc:  # noqa: BLE001 - never leak a traceback to clients
            log(f"internal error on {method} {self.path}: {exc!r}")
            try:
                self._send(500, {"error": "internal error"})
            except OSError:
                self.close_connection = True

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_DELETE(self):
        self._dispatch("DELETE")


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 32

    def __init__(self, address, handler, ssl_ctx: ssl.SSLContext | None):
        self.ssl_ctx = ssl_ctx
        if ":" in address[0]:
            self.address_family = socket.AF_INET6
        super().__init__(address, handler)

    def server_bind(self):
        if self.address_family == socket.AF_INET6:
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        super().server_bind()

    def get_request(self):
        sock, addr = super().get_request()
        sock.settimeout(120)
        return sock, addr

    def finish_request(self, request, client_address):
        # Do the TLS handshake in the worker thread so a slow client cannot
        # stall the accept loop.
        if self.ssl_ctx is None:
            return super().finish_request(request, client_address)
        try:
            tls = self.ssl_ctx.wrap_socket(request, server_side=True)
        except (ssl.SSLError, OSError):
            return None
        try:
            return super().finish_request(tls, client_address)
        finally:
            try:
                tls.close()
            except OSError:
                pass


def make_server(cfg: Config, state: AppState | None = None) -> Server:
    state = state or AppState(cfg)
    handler = type("BoundHandler", (Handler,), {"state": state})
    ctx = None
    if cfg.api.tls:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(cfg.api.tls_cert, cfg.api.tls_key)
    return Server((cfg.api.bind, cfg.api.port), handler, ctx)


def serve(cfg: Config) -> None:
    server = make_server(cfg)
    scheme = "https" if cfg.api.tls else "http"
    log(f"piagent {__version__} listening on {scheme}://{cfg.api.bind}:{cfg.api.port}")
    if server.RequestHandlerClass.state.admin_token() is None:
        log(f"warning: no valid token in {cfg.api.token_file}; admin endpoints will return 503")
    try:
        server.serve_forever()
    finally:
        server.server_close()
