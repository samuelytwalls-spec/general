"""The ``piagent`` command.

Most subcommands talk to the local API (so tools always execute inside the
sandboxed piagent service, as the unprivileged ``piagent`` user). Run them
as a member of the ``piagent`` group so the token can be read.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import ssl
import sys
import textwrap
import urllib.error
import urllib.request

from . import __version__
from .config import Config, load_config


class ApiClient:
    def __init__(self, cfg: Config, url: str | None = None, token: str | None = None):
        scheme = "https" if cfg.api.tls else "http"
        self.url = (url or os.environ.get("PIAGENT_URL") or f"{scheme}://127.0.0.1:{cfg.api.port}").rstrip("/")
        self.token = token or os.environ.get("PIAGENT_TOKEN") or self._read_token(cfg.api.token_file)
        handlers: list = [urllib.request.ProxyHandler({})]
        if self.url.startswith("https://"):
            ctx = ssl.create_default_context()
            if os.access(cfg.api.tls_cert, os.R_OK):
                ctx = ssl.create_default_context(cafile=cfg.api.tls_cert)
            handlers.append(urllib.request.HTTPSHandler(context=ctx))
        self.opener = urllib.request.build_opener(*handlers)

    @staticmethod
    def _read_token(path: str) -> str:
        try:
            with open(path) as fh:
                return fh.read().strip()
        except PermissionError:
            sys.exit(f"cannot read {path}: add your user to the 'piagent' group "
                     f"(sudo usermod -aG piagent $USER, then log in again) or use sudo")
        except FileNotFoundError:
            sys.exit(f"{path} does not exist yet; is piagent-firstboot.service finished?")

    def call(self, method: str, path: str, body: dict | None = None, timeout: float = 900) -> tuple[int, dict]:
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.url + path, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.token}")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with self.opener.open(req, timeout=timeout) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                return exc.code, json.loads(raw)
            except ValueError:
                return exc.code, {"error": raw.decode(errors="replace")}
        except (urllib.error.URLError, OSError) as exc:
            sys.exit(f"cannot reach the piagent API at {self.url}: {exc}\n"
                     f"check: systemctl status piagent-api")

    def ok(self, method: str, path: str, body: dict | None = None, timeout: float = 900) -> dict:
        status, payload = self.call(method, path, body, timeout)
        if status >= 400:
            extra = "".join(f"\n  - {r}" for r in payload.get("reasons", []))
            sys.exit(f"error {status}: {payload.get('error', payload)}{extra}")
        return payload


def _indent(text: str, prefix: str = "    ", limit: int = 1500) -> str:
    if len(text) > limit:
        text = text[:limit] + f"\n... ({len(text) - limit} more characters)"
    return textwrap.indent(text.rstrip(), prefix)


def _print_events(events: list[dict], verbose: bool) -> None:
    for ev in events:
        kind = ev.get("type")
        if kind == "tool_call":
            where = f" @ {ev['device']}" if ev.get("device") else ""
            if verbose and ev.get("thought"):
                print(f"  ({ev['thought']})")
            print(f"  -> {ev['tool']}{where}: {ev.get('input', '')}")
        elif kind == "tool_result" and verbose:
            print(_indent(ev.get("output", "")))
        elif kind == "refused":
            print(f"  !! refused by policy: {'; '.join(ev.get('reasons', []))}")
        elif kind == "confirmation":
            print(f"  {'approved' if ev['approved'] else 'rejected'}: {ev.get('input')}")
        elif kind == "error":
            print(f"  !! {ev.get('text')}")


def _confirm(pending: dict) -> bool:
    where = f"device '{pending['device']}'" if pending.get("device") else "this Pi (as user 'piagent')"
    print("\n  The agent wants to run a command that needs your approval:")
    print(f"      {pending['input']}")
    print(f"    on:  {where}")
    for reason in pending.get("reasons", []):
        print(f"    why: {reason}")
    if not sys.stdin.isatty():
        print("  (no terminal to ask on: rejecting)")
        return False
    try:
        answer = input("  Run it? [y/N] ").strip().lower()
    except EOFError:
        return False
    return answer in ("y", "yes")


def _converse(client: ApiClient, message: str, session_id: str | None, verbose: bool, auto_reject: bool) -> str:
    body = {"message": message}
    if session_id:
        body["session_id"] = session_id
    result = client.ok("POST", "/v1/chat", body)
    while True:
        _print_events(result.get("events", []), verbose)
        if result["status"] != "needs_confirmation":
            break
        pending = result["pending"]
        approve = False if auto_reject else _confirm(pending)
        result = client.ok("POST", f"/v1/sessions/{result['session_id']}/confirm",
                           {"approve": approve, "pending_id": pending["id"]})
    if result["status"] == "done":
        print("\n" + (result.get("answer") or "").strip() + "\n")
    else:
        print(f"\n[{result['status']}] {result.get('error') or ''}\n")
    return result["session_id"]


def cmd_chat(cfg: Config, args) -> None:
    client = ApiClient(cfg, args.url)
    print(f"PiAgent {__version__} - Phi-4-mini on this Pi. Type 'exit' to quit, 'new' for a fresh conversation.")
    session_id = None
    while True:
        try:
            line = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not line:
            continue
        if line in ("exit", "quit"):
            return
        if line == "new":
            session_id = None
            print("(new conversation)")
            continue
        session_id = _converse(client, line, session_id, args.verbose, False)


def cmd_ask(cfg: Config, args) -> None:
    client = ApiClient(cfg, args.url)
    _converse(client, " ".join(args.question), None, args.verbose, args.no_confirm)


def cmd_search(cfg: Config, args) -> None:
    client = ApiClient(cfg, args.url)
    res = client.ok("POST", "/v1/search", {"query": " ".join(args.query), "k": args.k})
    if not res["results"]:
        print("no results (are there files in /srv/docs, and has `piagent index` run?)")
    for i, hit in enumerate(res["results"], 1):
        print(f"[{i}] {hit['path']}  (similarity {hit['score']})")
        print(_indent(hit["text"], limit=600))


def cmd_index(cfg: Config, args) -> None:
    if args.local:
        import time

        from .llm import LlamaClient
        from .rag import DocIndex

        llm = LlamaClient(cfg.llm)
        deadline = time.monotonic() + args.wait
        while not llm.healthy("embed"):  # the model may still be loading after boot
            if time.monotonic() > deadline:
                sys.exit(f"embedding server at {cfg.llm.embed_url} is not ready (systemctl status llama-embed)")
            time.sleep(3)
        stats = DocIndex(cfg.rag, llm).index(full=args.full, log=print)
    else:
        stats = ApiClient(cfg, args.url).ok("POST", "/v1/index", {"full": args.full}, timeout=3600)
    print(json.dumps(stats, indent=2))


def cmd_status(cfg: Config, args) -> None:
    print(json.dumps(ApiClient(cfg, args.url).ok("GET", "/v1/status"), indent=2))


def cmd_serve(cfg: Config, args) -> None:
    from .api import serve

    serve(cfg)


def cmd_devices(cfg: Config, args) -> None:
    client = ApiClient(cfg, args.url)
    if args.action == "list":
        devs = client.ok("GET", "/v1/devices")["devices"]
        if not devs:
            print("no devices enrolled; add one with: piagent devices add NAME")
        for d in devs:
            state = "online " if d["online"] else "offline"
            print(f"{d['name']:<20} {state}  {d.get('platform') or '-':<10} {d.get('hostname') or '-':<20} "
                  f"pending={d['pending_commands']}")
    elif args.action == "add":
        res = client.ok("POST", "/v1/devices", {"name": args.name})
        conf = json.dumps(res["client_config"], indent=2)
        if args.output:
            fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as fh:
                fh.write(conf + "\n")
            print(f"client config for '{args.name}' written to {args.output} (contains a secret; copy it to the device)")
        else:
            print(conf)
            print(f"\nSave this as the client's config.json on '{args.name}'. The secret is not shown again.",
                  file=sys.stderr)
    elif args.action == "remove":
        client.ok("DELETE", f"/v1/devices/{args.name}")
        print(f"removed {args.name}")
    elif args.action == "run":
        command = " ".join(args.command)
        body = {"command": command, "timeout": args.timeout, "wait": args.wait}
        status, res = client.call("POST", f"/v1/devices/{args.name}/commands", body)
        if status == 409 and res.get("error") == "confirmation_required":
            if not _confirm({"input": command, "device": args.name, "reasons": res.get("reasons", [])}):
                sys.exit("not sent")
            res = client.ok("POST", f"/v1/devices/{args.name}/commands", {**body, "confirm": True})
        elif status >= 400:
            extra = "".join(f"\n  - {r}" for r in res.get("reasons", []))
            sys.exit(f"error {status}: {res.get('error')}{extra}")
        print(json.dumps(res, indent=2))


def cmd_token(cfg: Config, args) -> None:
    path = cfg.api.token_file
    if args.action == "show":
        print(ApiClient._read_token(path))
        return
    if os.geteuid() != 0:
        sys.exit("token rotate must run as root (sudo piagent token rotate)")
    import grp

    token = secrets.token_urlsafe(32)
    tmp = path + ".new"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o640)
    with os.fdopen(fd, "w") as fh:
        fh.write(token + "\n")
    os.chown(tmp, 0, grp.getgrnam("piagent").gr_gid)
    os.replace(tmp, path)
    print(token)
    print("token rotated; the API picks it up immediately", file=sys.stderr)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="piagent", description="Local LLM agent for the Raspberry Pi 5")
    p.add_argument("--config", help="config file (default /etc/piagent/piagent.toml)")
    p.add_argument("--url", help="API base URL (default: this Pi)")
    p.add_argument("--version", action="version", version=f"piagent {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("chat", help="interactive conversation")
    s.add_argument("-v", "--verbose", action="store_true", help="show tool output and reasoning")
    s.set_defaults(fn=cmd_chat)

    s = sub.add_parser("ask", help="one question, then exit")
    s.add_argument("question", nargs="+")
    s.add_argument("-v", "--verbose", action="store_true")
    s.add_argument("--no-confirm", action="store_true", help="reject every destructive command without asking")
    s.set_defaults(fn=cmd_ask)

    s = sub.add_parser("search", help="search /srv/docs")
    s.add_argument("query", nargs="+")
    s.add_argument("-k", type=int, default=4)
    s.set_defaults(fn=cmd_search)

    s = sub.add_parser("index", help="index /srv/docs for search")
    s.add_argument("--full", action="store_true", help="re-embed everything")
    s.add_argument("--local", action="store_true", help="index in-process instead of via the API (used by the timer)")
    s.add_argument("--wait", type=int, default=300, help="with --local: seconds to wait for the embedding server")
    s.set_defaults(fn=cmd_index)

    s = sub.add_parser("status", help="health of the models, index and API")
    s.set_defaults(fn=cmd_status)

    s = sub.add_parser("serve", help="run the HTTP API (systemd does this)")
    s.set_defaults(fn=cmd_serve)

    s = sub.add_parser("devices", help="manage remote devices")
    dsub = s.add_subparsers(dest="action", required=True)
    dsub.add_parser("list")
    d = dsub.add_parser("add")
    d.add_argument("name")
    d.add_argument("-o", "--output", help="write the client config to this file")
    d = dsub.add_parser("remove")
    d.add_argument("name")
    d = dsub.add_parser("run", help="queue a command for a device")
    d.add_argument("name")
    d.add_argument("command", nargs=argparse.REMAINDER)
    d.add_argument("--timeout", type=int, default=60)
    d.add_argument("--wait", type=int, default=60, help="seconds to wait for the result (0 = don't wait)")
    s.set_defaults(fn=cmd_devices)

    s = sub.add_parser("token", help="show or rotate the API token")
    s.add_argument("action", choices=["show", "rotate"])
    s.set_defaults(fn=cmd_token)
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    if getattr(args, "action", None) == "run" and getattr(args, "command", None) and args.command[0] == "--":
        args.command = args.command[1:]
    if getattr(args, "action", None) == "run" and not args.command:
        sys.exit("usage: piagent devices run NAME -- COMMAND ...")
    cfg = load_config(args.config)
    args.fn(cfg, args)
