#!/usr/bin/env python3
"""PiAgent companion client.

Runs on a Linux, macOS or Windows machine and executes *vetted* commands sent
by a PiAgent Raspberry Pi. It connects out to the Pi (no inbound port is
opened on this machine), and every command must pass four checks before it
runs:

1. the HMAC signature made with this device's secret is valid;
2. it is addressed to this device, unexpired, and not a replay;
3. it matches this machine's local policy (allowlist, deny rules, no shell
   metacharacters by default) -- the policy lives here, the Pi cannot change it;
4. optionally, a person at this machine approves it (``confirm_locally``).

Commands run without a shell, as the user running this client, with a
timeout and an output cap. Standard library only; Python 3.8+.

    python3 piagent_client.py init --from-json enrolled.json   # write config
    python3 piagent_client.py check "df -h"                    # test the policy
    python3 piagent_client.py test                             # test connectivity
    python3 piagent_client.py run                              # serve commands
"""

import argparse
import hashlib
import hmac
import http.client
import json
import locale
import os
import platform
import re
import shlex
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import time
from urllib.parse import urlparse

VERSION = "0.1.0"
IS_WINDOWS = os.name == "nt"

DEFAULT_POLICY = {
    "allow": [],
    "deny": [],
    "allow_shell_metachars": False,
    "disable_builtin_deny": False,
    "confirm_locally": False,
    "max_timeout": 120,
    "max_output_bytes": 65536,
    "max_clock_skew": 600,
    "workdir": None,
}

# Refused even when an allow rule matches (set disable_builtin_deny to opt out).
BUILTIN_DENY = [
    r"(?i)(^|[\s/\\])(sudo|su|doas|pkexec|runas|gsudo)(\.exe)?(\s|$)",
    r"(?i)(^|[\s/\\])(mkfs(\.\w+)?|fdisk|sfdisk|parted|wipefs|diskpart|format(\.com)?|diskutil)(\.exe)?(\s|$)",
    r"(?i)(^|[\s/\\])dd(\s|$)",
    r"(?i)(^|[\s/\\])(shutdown|reboot|poweroff|halt|init)(\.exe)?(\s|$)",
    r"(?i)(^|[\s/\\])rm\s+(-\w+\s+)*-\w*[rR]",
    r"(?i)\b(Format-Volume|Clear-Disk|Initialize-Disk|Remove-Partition|Stop-Computer|Restart-Computer)\b",
    r"(?i)\b(Remove-Item|rd|rmdir|del|erase)\b.*(\s/s\b|-Recurse)",
    r"(?i)\b(reg(\.exe)?\s+delete|bcdedit|vssadmin\s+delete|cipher\s+/w)\b",
]
METACHARS = re.compile(r"[;&|<>`$\r\n]|\$\(")


LOG_FILE = None


def log(msg):
    line = "%s %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    if LOG_FILE:
        with open(LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    elif sys.stderr:  # None under pythonw.exe
        print(line, file=sys.stderr, flush=True)


# -- configuration -------------------------------------------------------------
def default_config_dir():
    if IS_WINDOWS:
        return os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "piagent-client")
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support/piagent-client")
    if os.path.exists("/etc/piagent-client/config.json"):
        return "/etc/piagent-client"
    return os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "piagent-client")


def default_state_path(config_path):
    """state.json next to the config if writable, else in a per-user directory."""
    conf_dir = os.path.dirname(os.path.abspath(config_path))
    if os.access(conf_dir, os.W_OK):
        return os.path.join(conf_dir, "state.json")
    if IS_WINDOWS:
        base = os.environ.get("LOCALAPPDATA", os.path.expanduser("~"))
    else:
        base = os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state"))
    return os.path.join(base, "piagent-client", "state.json")


def load_config(path):
    with open(path, "r", encoding="utf-8") as fh:
        cfg = json.load(fh)
    for key in ("server", "device", "secret"):
        if not cfg.get(key):
            raise SystemExit("config %s is missing %r" % (path, key))
    policy = dict(DEFAULT_POLICY)
    policy.update(cfg.get("policy") or {})
    cfg["policy"] = policy
    for pattern in policy["allow"] + policy["deny"]:
        try:
            re.compile(pattern)
        except re.error as exc:
            raise SystemExit("invalid regex %r in policy: %s" % (pattern, exc))
    return cfg


def write_private(path, text):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)


# -- crypto (must match piagent/devices.py on the Pi) ------------------------------
def canonical_json(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def derive_keys(secret):
    key = secret.encode()
    auth = hmac.new(key, b"piagent-device-auth-v1", hashlib.sha256).hexdigest()
    sign = hmac.new(key, b"piagent-command-sign-v1", hashlib.sha256).digest()
    return auth, sign


# -- policy --------------------------------------------------------------------------
def vet(command, policy):
    """Return (allowed, reason) for a command line under the local policy."""
    if not isinstance(command, str) or not command.strip():
        return False, "empty command"
    if len(command) > 4096 or "\x00" in command:
        return False, "command too long or contains NUL"
    if not policy.get("allow_shell_metachars") and METACHARS.search(command):
        return False, "shell metacharacters are not allowed by this device's policy"
    if not policy.get("disable_builtin_deny"):
        for pattern in BUILTIN_DENY:
            if re.search(pattern, command):
                return False, "blocked by built-in deny rule"
    for pattern in policy.get("deny", []):
        if re.search(pattern, command):
            return False, "blocked by deny rule %r" % pattern
    for pattern in policy.get("allow", []):
        if re.fullmatch(pattern, command.strip()):
            return True, "allowed by rule %r" % pattern
    return False, "not in this device's allowlist"


def split_command(command):
    if IS_WINDOWS:
        return command  # CreateProcess takes the command line as-is (no shell)
    return shlex.split(command)


def _decode(data):
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode(locale.getpreferredencoding(False) or "latin-1", errors="replace")


def execute(command, timeout, policy):
    limit = int(policy.get("max_output_bytes", 65536))
    try:
        target = split_command(command)
    except ValueError as exc:
        return {"status": "rejected", "reason": "cannot parse command: %s" % exc}
    if not IS_WINDOWS and not shutil.which(target[0]):
        return {"status": "error", "reason": "command not found: %s" % target[0]}
    kwargs = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "cwd": policy.get("workdir") or None,
    }
    if IS_WINDOWS:
        kwargs["creationflags"] = 0x08000000 | 0x00000200  # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    started = time.monotonic()
    try:
        proc = subprocess.Popen(target, **kwargs)
    except OSError as exc:
        return {"status": "error", "reason": "could not start: %s" % exc}
    timed_out = False
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        if IS_WINDOWS:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        out, err = proc.communicate()
    return {
        "status": "done",
        "exit_code": None if timed_out else proc.returncode,
        "stdout": _decode(out[:limit]) + ("\n[output truncated]" if len(out) > limit else ""),
        "stderr": _decode(err[: limit // 4]) + ("\n[timed out]" if timed_out else ""),
        "reason": "timed out after %ss" % timeout if timed_out else "",
        "duration": round(time.monotonic() - started, 3),
    }


def confirm_locally(command):
    if not sys.stdin or not sys.stdin.isatty():
        return False
    print("\nPiAgent wants to run on this machine:\n    %s" % command)
    try:
        return input("Allow? [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


# -- HTTP ----------------------------------------------------------------------------
def normalize_fingerprint(fp):
    return re.sub(r"[^0-9a-f]", "", (fp or "").lower())


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS that trusts exactly one certificate, identified by its SHA-256."""

    def __init__(self, host, port, fingerprint, timeout):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE  # replaced by the fingerprint check below
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        http.client.HTTPSConnection.__init__(self, host, port, timeout=timeout, context=ctx)
        self._fingerprint = normalize_fingerprint(fingerprint)

    def connect(self):
        http.client.HTTPSConnection.connect(self)
        der = self.sock.getpeercert(binary_form=True)
        got = hashlib.sha256(der).hexdigest()
        if not hmac.compare_digest(got, self._fingerprint):
            self.sock.close()
            raise ssl.SSLError("server certificate fingerprint %s does not match the configured one" % got)


class Client:
    def __init__(self, cfg, config_path):
        self.cfg = cfg
        self.policy = cfg["policy"]
        self.device = cfg["device"]
        self.auth, self.sign_key = derive_keys(cfg["secret"])
        url = urlparse(cfg["server"])
        if url.scheme not in ("http", "https") or not url.hostname:
            raise SystemExit("server must look like https://piagent.local:8443")
        self.scheme = url.scheme
        self.host = url.hostname
        self.port = url.port or (443 if url.scheme == "https" else 80)
        self.base = url.path.rstrip("/")
        self.fingerprint = cfg.get("tls_fingerprint")
        if self.scheme == "http":
            log("warning: plain HTTP; commands are still signed, but traffic is readable on the LAN")
        self.stopping = False
        self.state_path = cfg.get("state_file") or default_state_path(config_path)
        self.seen = self._load_seen()

    def _load_seen(self):
        try:
            with open(self.state_path, "r", encoding="utf-8") as fh:
                return list(json.load(fh).get("seen", []))[-2000:]
        except (OSError, ValueError):
            return []

    def _save_seen(self):
        try:
            write_private(self.state_path, json.dumps({"seen": self.seen[-2000:]}))
        except OSError as exc:
            log("could not save state: %s" % exc)

    def _connection(self, timeout):
        if self.scheme == "http":
            return http.client.HTTPConnection(self.host, self.port, timeout=timeout)
        if self.fingerprint:
            return PinnedHTTPSConnection(self.host, self.port, self.fingerprint, timeout)
        ctx = ssl.create_default_context(cafile=self.cfg.get("ca_file") or None)
        return http.client.HTTPSConnection(self.host, self.port, timeout=timeout, context=ctx)

    def request(self, method, path, body=None, timeout=30):
        conn = self._connection(timeout)
        try:
            payload = json.dumps(body or {}).encode()
            conn.request(method, self.base + path, body=payload, headers={
                "Authorization": "Bearer " + self.auth,
                "X-PiAgent-Device": self.device,
                "Content-Type": "application/json",
                "User-Agent": "piagent-client/" + VERSION,
            })
            resp = conn.getresponse()
            data = resp.read()
            return resp.status, (json.loads(data) if data else None)
        finally:
            conn.close()

    def hello(self):
        summary = "allow=%s; metachars=%s; confirm_locally=%s" % (
            " | ".join(self.policy["allow"])[:700] or "(none)",
            self.policy["allow_shell_metachars"],
            self.policy["confirm_locally"],
        )
        status, body = self.request("POST", "/v1/agent/hello", {
            "hostname": socket.gethostname(),
            "platform": "%s-%s" % (platform.system().lower(), platform.machine().lower()),
            "version": VERSION,
            "policy": summary,
        })
        if status != 200:
            raise RuntimeError("hello failed: HTTP %s %s" % (status, body))
        return body

    def report(self, command_id, result):
        result = dict(result, id=command_id)
        status, body = self.request("POST", "/v1/agent/result", result)
        if status != 200:
            log("could not report result for %s: HTTP %s %s" % (command_id, status, body))

    def verify(self, item):
        env, sig = item.get("envelope"), item.get("signature", "")
        if not isinstance(env, dict) or not isinstance(sig, str):
            return None, "malformed command"
        expected = hmac.new(self.sign_key, canonical_json(env), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, sig):
            return None, "bad signature"
        if env.get("v") != 1 or env.get("device") != self.device:
            return None, "command is not addressed to this device"
        now, skew = time.time(), int(self.policy["max_clock_skew"])
        if env.get("expires_at", 0) + skew < now:
            return None, "command expired (check the clocks on both machines)"
        if env.get("issued_at", 0) - skew > now:
            return None, "command issued in the future (check the clocks on both machines)"
        if env.get("id") in self.seen:
            return None, "replayed command"
        return env, ""

    def handle(self, item):
        env, problem = self.verify(item)
        cid = (item.get("envelope") or {}).get("id")
        if env is None:
            log("REJECTED %s: %s" % (cid, problem))
            if cid and problem not in ("bad signature", "replayed command"):
                self.report(cid, {"status": "rejected", "reason": problem})
            return
        self.seen.append(env["id"])
        self._save_seen()
        command = env["command"]
        ok, reason = vet(command, self.policy)
        if not ok:
            log("REJECTED %s %r: %s" % (env["id"], command, reason))
            self.report(env["id"], {"status": "rejected", "reason": reason})
            return
        if self.policy.get("confirm_locally") and not confirm_locally(command):
            log("REJECTED %s %r: not approved locally" % (env["id"], command))
            self.report(env["id"], {"status": "rejected", "reason": "not approved by the local user"})
            return
        timeout = max(1, min(int(env.get("timeout", 60)), int(self.policy["max_timeout"])))
        log("RUN %s %r (timeout %ss)" % (env["id"], command, timeout))
        result = execute(command, timeout, self.policy)
        log("DONE %s status=%s exit=%s" % (env["id"], result.get("status"), result.get("exit_code")))
        self.report(env["id"], result)

    def run_forever(self, wait=25):
        backoff = 1
        greeted = False
        while not self.stopping:
            try:
                if not greeted:
                    self.hello()
                    greeted = True
                    log("connected to %s://%s:%s as %r" % (self.scheme, self.host, self.port, self.device))
                status, body = self.request("POST", "/v1/agent/poll", {"wait": wait}, timeout=wait + 20)
                if status == 204:
                    backoff = 1
                    continue
                if status == 401:
                    log("unauthorized: check the device name and secret (was the device removed on the Pi?)")
                    time.sleep(60)
                    continue
                if status != 200 or not isinstance(body, dict):
                    raise RuntimeError("poll failed: HTTP %s %s" % (status, body))
                self.handle(body)
                backoff = 1
            except KeyboardInterrupt:
                raise
            except Exception as exc:  # network trouble: retry with backoff
                log("connection problem: %s (retrying in %ss)" % (exc, backoff))
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
                greeted = False


# -- commands --------------------------------------------------------------------------
EXAMPLE_POLICIES = {
    "linux": [
        r"uptime", r"hostname", r"whoami", r"uname( -[a-z]+)?", r"free( -[hmg])?",
        r"df( -h)?( [A-Za-z0-9/._-]+)?", r"ip (-br )?(a|addr|route|link)", r"lsblk",
        r"systemctl status [A-Za-z0-9@._-]+( --no-pager)?", r"systemctl --failed( --no-pager)?",
        r"journalctl -u [A-Za-z0-9@._-]+ -n [0-9]{1,4} --no-pager", r"cat /etc/os-release",
        r"ps -eo pid,comm,%cpu,%mem --sort=-%cpu",
    ],
    "darwin": [
        r"uptime", r"hostname", r"whoami", r"uname( -[a-z]+)?", r"sw_vers", r"df( -h)?",
        r"vm_stat", r"pmset -g batt", r"system_profiler SPHardwareDataType", r"ifconfig( en[0-9])?",
    ],
    "windows": [
        r"(?i)hostname", r"(?i)whoami", r"(?i)ipconfig( /all)?", r"(?i)systeminfo", r"(?i)tasklist",
        r"(?i)powershell -NoProfile -Command Get-Volume",
        r"(?i)powershell -NoProfile -Command Get-ComputerInfo",
    ],
}


def cmd_init(args):
    if args.from_json:
        with open(args.from_json, "r", encoding="utf-8") as fh:
            enrolled = json.load(fh)
    else:
        enrolled = {"server": args.server, "device": args.device, "secret": args.secret,
                    "tls_fingerprint": args.fingerprint}
    for key in ("server", "device", "secret"):
        if not enrolled.get(key):
            raise SystemExit("missing %s (use --from-json FILE from `piagent devices add`, or --%s)" % (key, key))
    system = "windows" if IS_WINDOWS else ("darwin" if sys.platform == "darwin" else "linux")
    cfg = {
        "server": enrolled["server"],
        "device": enrolled["device"],
        "secret": enrolled["secret"],
        "tls_fingerprint": enrolled.get("tls_fingerprint"),
        "policy": dict(DEFAULT_POLICY, allow=EXAMPLE_POLICIES[system]),
    }
    path = args.config or os.path.join(default_config_dir(), "config.json")
    if os.path.exists(path) and not args.force:
        raise SystemExit("%s exists (use --force to overwrite)" % path)
    write_private(path, json.dumps(cfg, indent=2) + "\n")
    print("wrote %s with an example %s allowlist; review the policy before running." % (path, system))


def cmd_check(args, cfg):
    ok, reason = vet(args.command, cfg["policy"])
    print("%s: %s" % ("ALLOW" if ok else "REJECT", reason))
    sys.exit(0 if ok else 1)


def cmd_test(args, cfg, path):
    client = Client(cfg, path)
    info = client.hello()
    print("ok: authenticated to %s as %r (server clock offset %.1fs)"
          % (cfg["server"], cfg["device"], info.get("server_time", time.time()) - time.time()))


def main(argv=None):
    p = argparse.ArgumentParser(description="PiAgent companion client")
    p.add_argument("--config", help="config file (default: %s)" % os.path.join(default_config_dir(), "config.json"))
    p.add_argument("--log", help="append log lines to this file instead of stderr")
    p.add_argument("--version", action="version", version="piagent-client " + VERSION)
    sub = p.add_subparsers(dest="cmd")
    s = sub.add_parser("init", help="write a config file")
    s.add_argument("--from-json", help="client_config JSON printed by `piagent devices add`")
    s.add_argument("--server")
    s.add_argument("--device")
    s.add_argument("--secret")
    s.add_argument("--fingerprint")
    s.add_argument("--force", action="store_true")
    s = sub.add_parser("check", help="show whether the local policy allows a command")
    s.add_argument("command")
    sub.add_parser("test", help="check that the Pi accepts this device")
    s = sub.add_parser("run", help="wait for and execute commands (default)")
    s.add_argument("--wait", type=int, default=25, help="long-poll seconds")
    args = p.parse_args(argv)
    global LOG_FILE
    LOG_FILE = args.log

    if args.cmd == "init":
        return cmd_init(args)
    path = args.config or os.path.join(default_config_dir(), "config.json")
    if not os.path.exists(path):
        raise SystemExit("no config at %s; run `init` first" % path)
    cfg = load_config(path)
    if args.cmd == "check":
        return cmd_check(args, cfg)
    if args.cmd == "test":
        return cmd_test(args, cfg, path)
    if args.cmd is None:
        args.cmd = "run"
    if not IS_WINDOWS and hasattr(os, "geteuid") and os.geteuid() == 0:
        log("warning: running as root; prefer an unprivileged account (see install scripts)")
    try:
        Client(cfg, path).run_forever(wait=getattr(args, "wait", 25))
    except KeyboardInterrupt:
        log("stopped")


if __name__ == "__main__":
    main()
