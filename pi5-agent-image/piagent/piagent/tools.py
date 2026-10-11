"""Shell execution for the agent's ``shell`` tool."""

from __future__ import annotations

import os
import signal
import subprocess
import time
from dataclasses import asdict, dataclass

from .config import AgentConfig


@dataclass
class ShellResult:
    command: str
    exit_code: int | None
    stdout: str
    stderr: str
    duration: float
    timed_out: bool = False
    truncated: bool = False

    def to_dict(self) -> dict:
        return asdict(self)

    def observation(self) -> str:
        parts = [f"exit_code: {self.exit_code}"]
        if self.timed_out:
            parts.append("(timed out and was killed)")
        if self.stdout:
            parts.append("stdout:\n" + self.stdout)
        if self.stderr:
            parts.append("stderr:\n" + self.stderr)
        if not self.stdout and not self.stderr:
            parts.append("(no output)")
        return "\n".join(parts)


def clip(text: str, limit: int) -> tuple[str, bool]:
    """Keep the head and tail of long output."""
    if len(text) <= limit:
        return text, False
    head = limit * 2 // 3
    tail = limit - head
    omitted = len(text) - head - tail
    return f"{text[:head]}\n... [{omitted} characters omitted] ...\n{text[-tail:]}", True


class ShellTool:
    """Runs a command line with bash as the current (unprivileged) user."""

    def __init__(self, cfg: AgentConfig):
        self.cfg = cfg

    def _env(self) -> dict:
        home = os.environ.get("HOME", self.cfg.workdir)
        return {
            "PATH": "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
            "HOME": home,
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "TERM": "dumb",
            "PAGER": "cat",
            "SYSTEMD_PAGER": "",
            "GIT_PAGER": "cat",
        }

    def run(self, command: str, timeout: int | None = None) -> ShellResult:
        timeout = timeout or self.cfg.command_timeout
        os.makedirs(self.cfg.workdir, exist_ok=True)
        start = time.monotonic()
        proc = subprocess.Popen(
            [self.cfg.shell, "-c", command],
            cwd=self.cfg.workdir,
            env=self._env(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,  # own process group so a timeout kills children too
        )
        timed_out = False
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            out, err = proc.communicate()
        stdout, t1 = clip(out.decode(errors="replace"), self.cfg.max_output_chars)
        stderr, t2 = clip(err.decode(errors="replace"), self.cfg.max_output_chars // 2)
        return ShellResult(
            command=command,
            exit_code=None if timed_out else proc.returncode,
            stdout=stdout,
            stderr=stderr,
            duration=round(time.monotonic() - start, 3),
            timed_out=timed_out,
            truncated=t1 or t2,
        )
