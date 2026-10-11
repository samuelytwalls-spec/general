"""Configuration loading.

Settings come from /etc/piagent/piagent.toml (override the path with the
PIAGENT_CONFIG environment variable). Every key is optional; the defaults
below match the files shipped in the image.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields, is_dataclass

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - the image ships Python 3.11
    tomllib = None  # type: ignore[assignment]

DEFAULT_CONFIG_PATH = "/etc/piagent/piagent.toml"

LAN_NETWORKS = [
    "127.0.0.0/8",
    "::1/128",
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "169.254.0.0/16",
    "fc00::/7",
    "fe80::/10",
]


@dataclass
class LLMConfig:
    chat_url: str = "http://127.0.0.1:8081"
    embed_url: str = "http://127.0.0.1:8082"
    chat_model: str = "phi-4-mini"
    embed_model: str = "bge-small-en-v1.5"
    temperature: float = 0.2
    max_tokens: int = 512
    request_timeout: float = 600.0


@dataclass
class AgentConfig:
    max_steps: int = 8
    shell: str = "/bin/bash"
    workdir: str = "/var/lib/piagent/work"
    command_timeout: int = 60
    max_output_chars: int = 3000
    # Rough character budget for the prompt; Phi-4-mini runs with a 4k token
    # context and needs room for the reply.
    context_chars: int = 9000
    extra_confirm_patterns: list[str] = field(default_factory=list)
    extra_deny_patterns: list[str] = field(default_factory=list)


@dataclass
class RAGConfig:
    docs_dir: str = "/srv/docs"
    db_path: str = "/var/lib/piagent/rag.db"
    chunk_chars: int = 1200
    chunk_overlap: int = 200
    top_k: int = 4
    batch_size: int = 16
    max_file_bytes: int = 5_000_000
    query_prefix: str = "Represent this sentence for searching relevant passages: "
    extensions: list[str] = field(
        default_factory=lambda: [
            ".txt", ".md", ".rst", ".adoc", ".html", ".htm", ".csv", ".tsv",
            ".json", ".yaml", ".yml", ".toml", ".ini", ".conf", ".cfg",
            ".log", ".py", ".sh", ".pdf",
        ]
    )


@dataclass
class APIConfig:
    bind: str = "0.0.0.0"
    port: int = 8443
    token_file: str = "/etc/piagent/token"
    tls: bool = True
    tls_cert: str = "/etc/piagent/tls/cert.pem"
    tls_key: str = "/etc/piagent/tls/key.pem"
    allowed_networks: list[str] = field(default_factory=lambda: list(LAN_NETWORKS))
    session_ttl: int = 3600
    max_body_bytes: int = 1_000_000
    public_url: str = ""  # advertised to enrolled devices; derived if empty


@dataclass
class DevicesConfig:
    db_path: str = "/var/lib/piagent/devices.db"
    result_timeout: int = 120
    poll_wait_max: int = 30
    command_ttl: int = 600
    online_window: int = 90


@dataclass
class Config:
    llm: LLMConfig = field(default_factory=LLMConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    rag: RAGConfig = field(default_factory=RAGConfig)
    api: APIConfig = field(default_factory=APIConfig)
    devices: DevicesConfig = field(default_factory=DevicesConfig)


def _merge(obj, data: dict, where: str) -> None:
    known = {f.name: f for f in fields(obj)}
    for key, value in data.items():
        if key not in known:
            raise ValueError(f"unknown config key {where}{key}")
        current = getattr(obj, key)
        if is_dataclass(current):
            if not isinstance(value, dict):
                raise ValueError(f"config section {where}{key} must be a table")
            _merge(current, value, f"{where}{key}.")
        else:
            setattr(obj, key, value)


def load_config(path: str | None = None) -> Config:
    """Load the config file if it exists, falling back to defaults."""
    path = path or os.environ.get("PIAGENT_CONFIG", DEFAULT_CONFIG_PATH)
    cfg = Config()
    if path and os.path.exists(path):
        if tomllib is None:
            raise RuntimeError("Python 3.11+ is required to read TOML config")
        with open(path, "rb") as fh:
            _merge(cfg, tomllib.load(fh), "")
    return cfg
