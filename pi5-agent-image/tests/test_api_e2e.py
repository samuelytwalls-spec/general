"""End-to-end: real HTTPS API server, real companion client, fake LLM."""

import copy
import hashlib
import hmac
import http.client
import json
import ssl
import sys
import threading
import time

import pytest

import piagent_client as pc
from piagent.api import AppState, make_server
from piagent.devices import canonical_json, derive_keys

TOKEN = "t" * 43


class Api:
    def __init__(self, cfg):
        self.cfg = cfg
        self.state = AppState(cfg)
        self.server = make_server(cfg, self.state)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def call(self, method, path, body=None, token=TOKEN, headers=None, timeout=60):
        ctx = ssl.create_default_context(cafile=self.cfg.api.tls_cert)
        conn = http.client.HTTPSConnection("127.0.0.1", self.port, context=ctx, timeout=timeout)
        h = {"Content-Type": "application/json"}
        if token is not None:
            h["Authorization"] = "Bearer " + token
        h.update(headers or {})
        conn.request(method, path, body=None if body is None else json.dumps(body).encode(), headers=h)
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp.status, (json.loads(data) if data else None)

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def api(cfg):
    a = Api(cfg)
    yield a
    a.close()


def test_ping_needs_no_auth(api):
    assert api.call("GET", "/v1/ping", token=None) == (200, {"ok": True})


def test_admin_auth(api):
    assert api.call("GET", "/v1/status", token=None)[0] == 401
    assert api.call("GET", "/v1/status", token="wrong" * 10)[0] == 401
    status, body = api.call("GET", "/v1/status")
    assert status == 200 and body["chat_model_ready"] and body["tls_fingerprint"]


def test_failed_auth_is_throttled(api):
    for _ in range(10):
        assert api.call("GET", "/v1/status", token="nope" * 10)[0] == 401
    assert api.call("GET", "/v1/status")[0] == 429


def test_token_rotation_is_picked_up(api, cfg):
    with open(cfg.api.token_file, "w") as fh:
        fh.write("n" * 43)
    assert api.call("GET", "/v1/status")[0] == 401
    assert api.call("GET", "/v1/status", token="n" * 43)[0] == 200


def test_non_lan_clients_are_refused(cfg):
    cfg.api.allowed_networks = ["10.0.0.0/8"]
    a = Api(cfg)
    try:
        status, body = a.call("GET", "/v1/ping", token=None)
        assert status == 403 and "local network" in body["error"]
    finally:
        a.close()


def test_plain_http_is_rejected_on_tls_port(api):
    conn = http.client.HTTPConnection("127.0.0.1", api.port, timeout=5)
    with pytest.raises((http.client.HTTPException, OSError)):
        conn.request("GET", "/v1/ping")
        conn.getresponse().read()


def test_chat_with_confirmation_over_http(api, llama, cfg):
    llama.script = [
        {"tool": "shell", "input": "echo created > note.txt"},
        {"tool": "shell", "input": "cat note.txt"},
        {"tool": "final_answer", "input": "note.txt says created"},
    ]
    status, res = api.call("POST", "/v1/chat", {"message": "make a note"})
    assert status == 200 and res["status"] == "needs_confirmation"
    sid, pending = res["session_id"], res["pending"]
    assert "redirects output" in pending["reasons"][0]

    # a second chat on the same session must wait for the decision
    assert api.call("POST", "/v1/chat", {"message": "hi", "session_id": sid})[0] == 409
    assert api.call("POST", f"/v1/sessions/{sid}/confirm", {"approve": True, "pending_id": "wrong"})[0] == 409

    status, res = api.call("POST", f"/v1/sessions/{sid}/confirm", {"approve": True, "pending_id": pending["id"]})
    assert status == 200 and res["status"] == "done"
    outputs = [e["output"] for e in res["events"] if e["type"] == "tool_result"]
    assert any("created" in o for o in outputs)
    assert api.call("GET", f"/v1/sessions/{sid}")[1]["pending"] is None


def test_bad_requests(api):
    assert api.call("POST", "/v1/chat", {"message": ""})[0] == 400
    assert api.call("POST", "/v1/search", {"query": "x", "k": 99})[0] == 400
    assert api.call("GET", "/v1/nope")[0] == 404
    assert api.call("DELETE", "/v1/chat")[0] == 405


# -- remote devices --------------------------------------------------------------

def enroll(api, tmp_path, name="laptop", allow=None):
    status, res = api.call("POST", "/v1/devices", {"name": name})
    assert status == 201
    conf = res["client_config"]
    fp = conf["tls_fingerprint"]
    assert fp and len(fp.replace(":", "")) == 64
    conf["server"] = f"https://127.0.0.1:{api.port}"
    conf["policy"] = dict(pc.DEFAULT_POLICY, allow=allow if allow is not None else [r"echo [a-z-]+", r"uname( -a)?"])
    path = tmp_path / f"{name}.json"
    path.write_text(json.dumps(conf))
    return pc.load_config(str(path)), str(path)


@pytest.fixture
def device(api, tmp_path):
    ccfg, path = enroll(api, tmp_path)
    client = pc.Client(ccfg, path)
    t = threading.Thread(target=client.run_forever, kwargs={"wait": 1}, daemon=True)
    t.start()
    deadline = time.time() + 10
    while time.time() < deadline and not api.call("GET", "/v1/devices")[1]["devices"][0]["online"]:
        time.sleep(0.1)
    yield client
    client.stopping = True
    t.join(timeout=10)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX commands")
def test_agent_runs_vetted_command_on_device(api, llama, device):
    llama.script = [
        {"tool": "list_devices", "input": ""},
        {"tool": "remote_exec", "device": "laptop", "input": "echo hello-remote"},
        {"tool": "final_answer", "input": "done"},
    ]
    status, res = api.call("POST", "/v1/chat", {"message": "say hello on my laptop"})
    assert status == 200 and res["status"] == "done", res
    listing = [e for e in res["events"] if e.get("tool") == "list_devices" and e["type"] == "tool_result"][0]
    assert "laptop: online" in listing["output"] and "echo [a-z-]+" in listing["output"]
    remote = [e for e in res["events"] if e.get("tool") == "remote_exec" and e["type"] == "tool_result"][0]
    assert remote["status"] == "done" and "hello-remote" in remote["output"]


def test_device_policy_rejects_unlisted_command(api, device):
    status, res = api.call("POST", "/v1/devices/laptop/commands", {"command": "cat /etc/hostname", "wait": 10})
    assert status == 200
    assert res["status"] == "rejected" and "allowlist" in res["reason"]


def test_device_policy_rejects_metacharacters(api, device):
    status, res = api.call("POST", "/v1/devices/laptop/commands", {"command": "echo hi; uname", "wait": 10})
    assert res["status"] == "rejected" and "metacharacters" in res["reason"]


def test_pi_side_vetting_for_device_commands(api, device):
    status, res = api.call("POST", "/v1/devices/laptop/commands", {"command": "sudo ls"})
    assert status == 403
    status, res = api.call("POST", "/v1/devices/laptop/commands", {"command": "rm -r build"})
    assert status == 409 and res["error"] == "confirmation_required"
    status, res = api.call("POST", "/v1/devices/laptop/commands", {"command": "rm -r build", "confirm": True, "wait": 10})
    assert status == 200 and res["status"] == "rejected"  # the device's own policy still says no


def test_device_auth(api, tmp_path):
    ccfg, _ = enroll(api, tmp_path, "desk")
    auth, _ = derive_keys(ccfg["secret"])
    hdr = {"X-PiAgent-Device": "desk"}
    assert api.call("POST", "/v1/agent/poll", {"wait": 0}, token=auth, headers=hdr)[0] == 204
    assert api.call("POST", "/v1/agent/poll", {"wait": 0}, token=TOKEN, headers=hdr)[0] == 401
    # device credentials do not open admin endpoints
    assert api.call("GET", "/v1/devices", token=auth)[0] == 401


def test_wrong_fingerprint_is_refused(api, tmp_path):
    ccfg, path = enroll(api, tmp_path, "evil")
    ccfg["tls_fingerprint"] = "00" * 32
    with pytest.raises(ssl.SSLError):
        pc.Client(ccfg, path).hello()


def test_envelope_verification(api, tmp_path):
    ccfg, path = enroll(api, tmp_path, "unit")
    client = pc.Client(ccfg, path)
    _, sign = derive_keys(ccfg["secret"])
    now = int(time.time())
    env = {"v": 1, "id": "a" * 32, "device": "unit", "command": "echo ok", "timeout": 5,
           "issued_at": now, "expires_at": now + 60, "nonce": "n"}

    def signed(e):
        return {"envelope": e, "signature": hmac.new(sign, canonical_json(e), hashlib.sha256).hexdigest()}

    assert client.verify(signed(env))[0] == env
    tampered = signed(env)
    tampered["envelope"] = dict(env, command="echo pwned")
    assert client.verify(tampered) == (None, "bad signature")
    assert "not addressed" in client.verify(signed(dict(env, device="other")))[1]
    assert "expired" in client.verify(signed(dict(env, issued_at=now - 5000, expires_at=now - 4000)))[1]
    client.seen.append(env["id"])
    assert client.verify(signed(env)) == (None, "replayed command")


def test_client_vet_rules():
    policy = dict(pc.DEFAULT_POLICY, allow=[r"df( -h)?", r"sudo ls", r".*"])
    assert pc.vet("df -h", policy)[0]
    assert not pc.vet("df -h | tee x", policy)[0]
    assert not pc.vet("sudo ls", policy)[0]  # built-in deny wins over allow
    assert not pc.vet("shutdown /s /t 0", policy)[0]
    assert not pc.vet("format C:", policy)[0]
    strict = copy.deepcopy(pc.DEFAULT_POLICY)
    assert pc.vet("df -h", strict) == (False, "not in this device's allowlist")
