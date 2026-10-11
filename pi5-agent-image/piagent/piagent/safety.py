"""Classify shell commands as safe, needing confirmation, or denied.

This is a guard rail, not a sandbox: the agent already runs as the
unprivileged ``piagent`` user under systemd hardening. The classifier makes
sure a human approves anything that deletes, overwrites, kills, installs or
reconfigures, and refuses a few things the agent should never attempt
(privilege escalation, disk formatting, fork bombs).

Anything the classifier cannot parse is treated as needing confirmation.
"""

from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass, field

SAFE = "safe"
CONFIRM = "confirm"
DENY = "deny"
_RANK = {SAFE: 0, CONFIRM: 1, DENY: 2}


@dataclass
class Verdict:
    level: str = SAFE
    reasons: list[str] = field(default_factory=list)

    def add(self, level: str, reason: str) -> None:
        if _RANK[level] > _RANK[self.level]:
            self.level = level
        if reason not in self.reasons:
            self.reasons.append(reason)

    def merge(self, other: "Verdict") -> None:
        for reason in other.reasons:
            if reason not in self.reasons:
                self.reasons.append(reason)
        if _RANK[other.level] > _RANK[self.level]:
            self.level = other.level


# Never allowed from the agent.
DENY_COMMANDS = {
    "sudo": "privilege escalation is not allowed",
    "su": "privilege escalation is not allowed",
    "doas": "privilege escalation is not allowed",
    "pkexec": "privilege escalation is not allowed",
    "run0": "privilege escalation is not allowed",
    "visudo": "editing sudoers is not allowed",
    "mkfs": "formatting filesystems is not allowed",
    "mke2fs": "formatting filesystems is not allowed",
    "mkswap": "formatting swap is not allowed",
    "wipefs": "wiping disks is not allowed",
    "fdisk": "partitioning disks is not allowed",
    "sfdisk": "partitioning disks is not allowed",
    "gdisk": "partitioning disks is not allowed",
    "sgdisk": "partitioning disks is not allowed",
    "cfdisk": "partitioning disks is not allowed",
    "parted": "partitioning disks is not allowed",
    "flashrom": "flashing firmware is not allowed",
}

# Always need a human "yes".
CONFIRM_COMMANDS = {
    "rm": "deletes files",
    "rmdir": "deletes directories",
    "unlink": "deletes files",
    "shred": "destroys file contents",
    "dd": "raw copy that can overwrite data",
    "truncate": "truncates files",
    "mv": "moves or overwrites files",
    "cp": "can overwrite files",
    "install": "can overwrite files",
    "tee": "writes files",
    "ln": "creates or replaces links",
    "chmod": "changes permissions",
    "chown": "changes ownership",
    "chgrp": "changes ownership",
    "chattr": "changes file attributes",
    "setfacl": "changes permissions",
    "kill": "terminates processes",
    "killall": "terminates processes",
    "pkill": "terminates processes",
    "shutdown": "powers off or reboots",
    "reboot": "reboots",
    "poweroff": "powers off",
    "halt": "halts the system",
    "init": "changes runlevel",
    "telinit": "changes runlevel",
    "useradd": "modifies user accounts",
    "userdel": "modifies user accounts",
    "usermod": "modifies user accounts",
    "groupadd": "modifies groups",
    "groupdel": "modifies groups",
    "groupmod": "modifies groups",
    "passwd": "changes passwords",
    "chpasswd": "changes passwords",
    "mount": "changes mounts",
    "umount": "changes mounts",
    "swapon": "changes swap",
    "swapoff": "changes swap",
    "iptables": "changes the firewall",
    "ip6tables": "changes the firewall",
    "nft": "changes the firewall",
    "ufw": "changes the firewall",
    "eval": "evaluates arbitrary code",
    "source": "runs arbitrary code",
    ".": "runs arbitrary code",
    "exec": "replaces the process",
    "patch": "modifies files",
    "rpi-eeprom-update": "changes bootloader firmware",
    "rpi-update": "changes kernel/firmware",
    "raspi-config": "changes system configuration",
    "crontab": "changes scheduled jobs",
    "at": "schedules commands",
    "unzip": "can overwrite files",
    "wget": "downloads files",
}

# Commands where only some subcommands/flags are risky.
_SUBCOMMAND_RULES = {
    "systemctl": (
        {"start", "stop", "restart", "reload", "try-restart", "reload-or-restart",
         "enable", "disable", "reenable", "mask", "unmask", "kill", "isolate",
         "poweroff", "reboot", "halt", "suspend", "hibernate", "daemon-reload",
         "edit", "set-property", "revert", "link", "preset", "set-default",
         "rescue", "emergency", "default", "kexec", "soft-reboot"},
        "changes services or power state",
    ),
    "service": ({"start", "stop", "restart", "reload", "force-reload"}, "changes services"),
    "apt": ({"install", "remove", "purge", "upgrade", "full-upgrade", "dist-upgrade",
             "autoremove", "reinstall", "update", "edit-sources"}, "changes installed packages"),
    "apt-get": ({"install", "remove", "purge", "upgrade", "dist-upgrade", "autoremove",
                 "update", "clean", "autoclean", "build-dep", "source"}, "changes installed packages"),
    "dpkg": ({"-i", "--install", "-r", "--remove", "-P", "--purge", "--configure",
              "--unpack", "--set-selections", "--clear-selections"}, "changes installed packages"),
    "pip": ({"install", "uninstall", "download", "wheel"}, "changes Python packages"),
    "pip3": ({"install", "uninstall", "download", "wheel"}, "changes Python packages"),
    "npm": ({"install", "i", "uninstall", "remove", "rm", "update", "ci", "link", "publish"},
            "changes Node packages"),
    "docker": ({"rm", "rmi", "kill", "stop", "restart", "prune", "run", "exec", "pull",
                "push", "system", "volume", "network", "build", "compose"}, "changes containers"),
    "git": ({"reset", "clean", "push", "checkout", "restore", "rebase", "merge", "pull",
             "commit", "stash", "rm", "mv", "branch", "tag", "gc", "filter-branch",
             "switch", "cherry-pick", "revert", "am", "apply", "worktree", "clone",
             "init", "config"}, "changes a git repository"),
    "ip": ({"link", "addr", "address", "a", "route", "r", "rule", "neigh", "n",
            "tunnel", "netns", "xfrm", "maddr", "mroute"}, "may change network configuration"),
    "hostnamectl": ({"set-hostname", "hostname", "set-icon-name", "set-chassis",
                     "set-deployment", "set-location"}, "changes the hostname"),
    "timedatectl": ({"set-time", "set-timezone", "set-ntp", "set-local-rtc"}, "changes time settings"),
    "loginctl": ({"terminate-session", "terminate-user", "kill-session", "kill-user",
                  "poweroff", "reboot"}, "ends sessions or powers off"),
    "journalctl": ({"--vacuum-size", "--vacuum-time", "--vacuum-files", "--rotate",
                    "--flush", "--relinquish-var"}, "deletes logs"),
    "vcgencmd": ({"set_logging"}, "changes firmware settings"),
}

# `ip` read-only forms: `ip addr`, `ip a show`, `ip route` ... only risky with
# these verbs.
_IP_WRITE_VERBS = {"add", "del", "delete", "change", "replace", "set", "flush", "append", "prepend"}
_NMCLI_WRITE_VERBS = {"up", "down", "delete", "del", "modify", "mod", "add", "edit", "clone",
                      "import", "load", "reload", "connect", "disconnect", "reapply", "set",
                      "on", "off", "hotspot", "hostname", "permissions", "logging"}

# Wrappers whose first non-option argument is the real command.
_WRAPPERS = {"nohup", "nice", "ionice", "time", "timeout", "stdbuf", "setsid", "env",
             "command", "builtin", "xargs", "watch", "chrt", "taskset", "unbuffer", "caffeinate"}
# Wrapper options that consume a value.
_WRAPPER_VALUE_OPTS = {
    "nice": {"-n", "--adjustment"},
    "ionice": {"-c", "-n", "-p", "--class", "--classdata"},
    "timeout": {"-s", "--signal", "-k", "--kill-after"},
    "stdbuf": {"-i", "-o", "-e"},
    "env": {"-u", "--unset", "-C", "--chdir", "-S", "--split-string"},
    "xargs": {"-a", "-d", "-E", "-I", "-L", "-n", "-P", "-s", "--arg-file",
              "--delimiter", "--max-args", "--max-procs", "--max-chars", "--replace"},
    "watch": {"-n", "--interval", "-d"},
    "chrt": {"-p"},
    "taskset": {"-c", "-p"},
}
# `timeout 10 cmd`, `chrt 10 cmd`, `taskset 0x3 cmd`: first positional is a value.
_WRAPPER_POSITIONAL_VALUE = {"timeout", "chrt", "taskset"}

_SHELLS = {"sh", "bash", "dash", "zsh", "ksh", "fish", "busybox"}
_INTERPRETERS = {"python", "python3", "perl", "ruby", "node", "nodejs", "php", "lua",
                 "lua5.1", "luajit", "tclsh", "osascript", "powershell", "pwsh"}
_AWKS = {"awk", "gawk", "mawk", "nawk", "busybox-awk"}
# system("..."), "cmd" | getline, print > "file", print | "cmd"
_AWK_RISKY = re.compile(r"\bsystem\s*\(|\|\s*&?\s*getline|\bprintf?\b[^;}]*?(>|\|(?!\|))")

_BLOCK_DEVICE = re.compile(r"^/dev/(sd[a-z]|mmcblk|nvme|hd[a-z]|vd[a-z]|xvd|loop|mtd|disk/|mapper/|md\d|dm-|ram)")
_SYSTEM_PATH = re.compile(
    r"^(/|/\*|~|~/|~/\*|\$HOME/?\*?|/(bin|boot|dev|etc|home|lib|lib64|opt|proc|root|run|sbin|srv|sys|usr|var)/?\*?)$"
)
_FORK_BOMB = re.compile(r":\s*\(\s*\)\s*\{[^}]*:\s*\|\s*:\s*&")
_SEPARATORS = {";", ";;", "&", "&&", "|", "||", "|&", "(", ")", "{", "}", "!"}


def _tokenize(command: str) -> list[str]:
    lex = shlex.shlex(command.replace("\n", " ; "), posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    lex.commenters = ""  # keep everything visible to the classifier
    return list(lex)


def _segments(tokens: list[str]) -> tuple[list[list[str]], list[tuple[str, str]]]:
    """Split tokens into simple commands and collect redirections."""
    segments: list[list[str]] = [[]]
    redirects: list[tuple[str, str]] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        is_punct = bool(tok) and all(c in "();<>|&" for c in tok)
        if is_punct and (">" in tok or "<" in tok):
            target = tokens[i + 1] if i + 1 < len(tokens) else ""
            redirects.append((tok, target))
            i += 2
            continue
        if is_punct or tok in _SEPARATORS:
            segments.append([])
        else:
            segments[-1].append(tok)
        i += 1
    return [s for s in segments if s], redirects


def _unwrap(argv: list[str]) -> list[str]:
    """Strip env assignments and wrapper commands (nohup, timeout 5, xargs ...)."""
    while argv:
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", argv[0]):
            argv = argv[1:]
            continue
        name = os.path.basename(argv[0])
        if name not in _WRAPPERS:
            return argv
        rest = argv[1:]
        value_opts = _WRAPPER_VALUE_OPTS.get(name, set())
        while rest and rest[0].startswith("-"):
            opt = rest.pop(0)
            if opt in value_opts and rest:
                rest.pop(0)
            if opt == "--":
                break
        if name == "env":
            while rest and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", rest[0]):
                rest.pop(0)
        if name in _WRAPPER_POSITIONAL_VALUE and rest:
            rest.pop(0)
        argv = rest
    return argv


def _classify_argv(argv: list[str], verdict: Verdict, depth: int) -> None:
    argv = _unwrap(argv)
    if not argv:
        return
    cmd = argv[0]
    name = os.path.basename(cmd)
    args = argv[1:]

    if cmd.startswith("$") or "`" in cmd:
        verdict.add(CONFIRM, f"command name comes from a variable ({cmd})")
        return
    if name.startswith("mkfs") or name in DENY_COMMANDS:
        verdict.add(DENY, f"{name}: {DENY_COMMANDS.get(name, 'formatting filesystems is not allowed')}")
        return

    if name in _SHELLS:
        if "-c" in args:
            idx = args.index("-c")
            if idx + 1 < len(args) and depth < 3:
                verdict.merge(classify(args[idx + 1], _depth=depth + 1))
                return
        if any(not a.startswith("-") for a in args) or not args:
            verdict.add(CONFIRM, f"{name}: runs a script or an interactive shell")
        return
    if name in _AWKS:
        # awk is mostly used to pick columns; it is only risky when the program
        # runs commands (system(), "cmd" | getline, print | "cmd"), writes files
        # (print > "file") or edits in place (gawk -i inplace), or comes from a file.
        if any(_AWK_RISKY.search(a) or a in ("-i", "-f") or a.startswith(("--include", "--file", "-i", "-f"))
               or "inplace" in a for a in args):
            verdict.add(CONFIRM, f"{name}: program can run commands or write files")
        return
    if name in _INTERPRETERS or re.match(r"^python3(\.\d+)?$", name):
        if any(a in ("-c", "-e", "-E", "--eval", "-Command", "-EncodedCommand") for a in args) or any(
            not a.startswith("-") for a in args
        ):
            verdict.add(CONFIRM, f"{name}: runs arbitrary code")
        return

    if name == "rm":
        recursive = any(
            a in ("-r", "-R", "--recursive") or (re.match(r"^-[a-zA-Z]+$", a) and ("r" in a or "R" in a))
            for a in args
        )
        targets = [a for a in args if not a.startswith("-")]
        if "--no-preserve-root" in args or (recursive and any(_SYSTEM_PATH.match(t) for t in targets)):
            verdict.add(DENY, "rm: recursive delete of a system or home directory")
            return
    if name == "dd":
        for a in args:
            if a.startswith("of=") and _BLOCK_DEVICE.match(a[3:]):
                verdict.add(DENY, "dd: writing to a block device is not allowed")
                return
    if name == "shred" and any(_BLOCK_DEVICE.match(a) for a in args):
        verdict.add(DENY, "shred: wiping a block device is not allowed")
        return
    if name in ("chmod", "chown", "chgrp") and any(a in ("-R", "--recursive") for a in args):
        if any(_SYSTEM_PATH.match(a) for a in args if not a.startswith("-")):
            verdict.add(DENY, f"{name}: recursive change on a system directory")
            return

    if name in CONFIRM_COMMANDS:
        if name == "ln" and not any(a in ("-f", "--force") or (a.startswith("-") and "f" in a) for a in args):
            return
        if name == "crontab" and not any(a in ("-r", "-e", "-i") or not a.startswith("-") for a in args):
            return
        verdict.add(CONFIRM, f"{name}: {CONFIRM_COMMANDS[name]}")
        return

    if name in _SUBCOMMAND_RULES:
        risky, reason = _SUBCOMMAND_RULES[name]
        positional = [a for a in args if not a.startswith("-") or a in risky]
        if name == "ip":
            if any(a in _IP_WRITE_VERBS for a in args):
                verdict.add(CONFIRM, f"ip: {reason}")
            return
        if name in ("dpkg", "journalctl"):
            if any(a.split("=")[0] in risky for a in args):
                verdict.add(CONFIRM, f"{name}: {reason}")
            return
        if name == "git" and positional[:1] == ["branch"] and not any(
            a in ("-d", "-D", "--delete", "-m", "-M", "--move", "-f", "--force") for a in args
        ):
            return
        if name == "git" and positional[:1] == ["stash"] and positional[1:2] in (["list"], ["show"]):
            return
        if name == "git" and positional[:1] == ["config"] and not any(
            a in ("--add", "--unset", "--unset-all", "--replace-all", "--rename-section", "--remove-section", "-e", "--edit")
            for a in args
        ) and len(positional) < 3:
            return
        if positional and positional[0] in risky:
            verdict.add(CONFIRM, f"{name} {positional[0]}: {reason}")
        return

    if name == "nmcli":
        if any(a in _NMCLI_WRITE_VERBS for a in args):
            verdict.add(CONFIRM, "nmcli: changes network configuration")
        return
    if name == "sed" and any(a == "-i" or a.startswith("-i") or a.startswith("--in-place") for a in args):
        verdict.add(CONFIRM, "sed -i: edits files in place")
    elif name == "perl" and any(a.startswith("-i") or a.startswith("-pi") for a in args):
        verdict.add(CONFIRM, "perl -i: edits files in place")
    elif name == "find":
        if "-delete" in args:
            verdict.add(CONFIRM, "find -delete: deletes files")
        for flag in ("-exec", "-execdir", "-ok", "-okdir"):
            if flag in args:
                idx = args.index(flag)
                inner = [a for a in args[idx + 1:] if a not in ("{}", ";", "+", "\\;")]
                sub = Verdict()
                _classify_argv(inner, sub, depth + 1)
                if sub.level == SAFE:
                    continue
                verdict.merge(sub)
    elif name == "curl" and any(
        a in ("-o", "-O", "--output", "--remote-name", "-T", "--upload-file", "-X", "--request", "-d", "--data")
        or a.startswith("--data") or a.startswith("--output") or (re.match(r"^-[a-zA-Z]+$", a) and ("o" in a or "O" in a))
        for a in args
    ):
        verdict.add(CONFIRM, "curl: writes files or sends data")
    elif name == "tar" and any(
        a in ("--extract", "--get", "--delete", "--create", "--append", "--update")
        or (re.match(r"^-[a-zA-Z]+$", a) and any(c in a for c in "xcru"))
        or (i == 0 and re.match(r"^[a-zA-Z]+$", a) and any(c in a for c in "xcru"))
        for i, a in enumerate(args)
    ):
        verdict.add(CONFIRM, "tar: creates or extracts archives (can overwrite files)")
    elif name == "rsync" and any(a.startswith("--delete") or a.startswith("--remove-source") for a in args):
        verdict.add(CONFIRM, "rsync --delete: deletes files")
    elif name in ("truncate", "fallocate"):
        verdict.add(CONFIRM, f"{name}: changes file sizes")


def classify(
    command: str,
    extra_confirm: list[str] | tuple[str, ...] = (),
    extra_deny: list[str] | tuple[str, ...] = (),
    _depth: int = 0,
) -> Verdict:
    """Return the verdict for a shell command line."""
    verdict = Verdict()
    text = command.strip()
    if not text:
        verdict.add(DENY, "empty command")
        return verdict

    for pattern in extra_deny:
        if re.search(pattern, text):
            verdict.add(DENY, f"matches deny rule {pattern!r}")
    for pattern in extra_confirm:
        if re.search(pattern, text):
            verdict.add(CONFIRM, f"matches confirm rule {pattern!r}")

    if _FORK_BOMB.search(text):
        verdict.add(DENY, "fork bomb")
        return verdict
    if "$(" in text or "`" in text or "<(" in text or ">(" in text:
        verdict.add(CONFIRM, "uses command/process substitution, which can hide what runs")

    try:
        tokens = _tokenize(text)
    except ValueError as exc:
        verdict.add(CONFIRM, f"could not parse the command ({exc})")
        return verdict

    segments, redirects = _segments(tokens)
    for op, target in redirects:
        if "<" in op and ">" not in op:
            continue  # input redirection / here-doc
        if op.endswith("&") and (target.isdigit() or target == "-"):
            continue  # fd duplication such as 2>&1
        if target.startswith("&") or target in ("/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty") or target.startswith("/dev/fd/"):
            continue
        if _BLOCK_DEVICE.match(target):
            verdict.add(DENY, f"writes directly to block device {target}")
        else:
            verdict.add(CONFIRM, f"redirects output into {target or 'a file'} (creates or overwrites it)")

    for argv in segments:
        _classify_argv(argv, verdict, _depth)
    return verdict


def is_destructive(command: str) -> bool:
    return classify(command).level != SAFE


__all__ = ["SAFE", "CONFIRM", "DENY", "Verdict", "classify", "is_destructive"]
