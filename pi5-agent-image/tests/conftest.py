"""Shared fixtures: a fake llama.cpp server and a throwaway config.

The fake server speaks the two endpoints piagent uses (/v1/chat/completions
and /v1/embeddings) so the real HTTP client code is exercised. Chat replies
come from a per-test script; embeddings are a deterministic bag-of-words
hash, which is enough to make retrieval tests meaningful.
"""

import hashlib
import json
import math
import os
import re
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "piagent"))
sys.path.insert(0, os.path.join(ROOT, "client"))

from piagent.config import Config  # noqa: E402

DIM = 64


def fake_embedding(text: str) -> list[float]:
    vec = [0.0] * DIM
    for word in re.findall(r"[a-z0-9]+", text.lower()):
        h = int(hashlib.sha256(word.encode()).hexdigest(), 16)
        vec[h % DIM] += 1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


class FakeLlama:
    def __init__(self):
        self.script: list = []
        self.requests: list = []
        self.lock = threading.Lock()
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _json(self, code, obj):
                data = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self._json(200, {"status": "ok"})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                with fake.lock:
                    fake.requests.append((self.path, body))
                if self.path == "/v1/embeddings":
                    data = [{"index": i, "embedding": fake_embedding(t)} for i, t in enumerate(body["input"])]
                    return self._json(200, {"data": data})
                if self.path == "/v1/chat/completions":
                    with fake.lock:
                        item = fake.script.pop(0) if fake.script else {"tool": "final_answer", "input": "(script empty)"}
                    if callable(item):
                        item = item(body)
                    finish = "stop"
                    if isinstance(item, dict) and item.get("_truncate"):
                        finish, item = "length", '{"thought": "cut off mid-'
                    content = item if isinstance(item, str) else json.dumps({"thought": "t", **item})
                    return self._json(200, {"choices": [{"message": {"role": "assistant", "content": content},
                                                         "finish_reason": finish}]})
                self._json(404, {"error": "nope"})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def chat_requests(self):
        return [b for p, b in self.requests if p == "/v1/chat/completions"]


@pytest.fixture
def llama():
    fake = FakeLlama()
    yield fake
    fake.server.shutdown()


@pytest.fixture
def cfg(tmp_path, llama):
    c = Config()
    c.llm.chat_url = llama.url
    c.llm.embed_url = llama.url
    c.llm.request_timeout = 10
    c.agent.workdir = str(tmp_path / "work")
    c.agent.command_timeout = 10
    c.rag.docs_dir = str(tmp_path / "docs")
    c.rag.db_path = str(tmp_path / "state" / "rag.db")
    c.rag.query_prefix = ""
    c.devices.db_path = str(tmp_path / "state" / "devices.db")
    c.devices.result_timeout = 15
    c.devices.poll_wait_max = 2
    c.api.bind = "127.0.0.1"
    c.api.port = 0
    c.api.token_file = str(tmp_path / "token")
    c.api.tls_cert = str(tmp_path / "tls" / "cert.pem")
    c.api.tls_key = str(tmp_path / "tls" / "key.pem")
    os.makedirs(c.rag.docs_dir)
    with open(c.api.token_file, "w") as fh:
        fh.write("t" * 43 + "\n")
    os.makedirs(tmp_path / "tls")
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
         "-nodes", "-days", "2", "-subj", "/CN=piagent-test",
         "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1",
         "-keyout", c.api.tls_key, "-out", c.api.tls_cert],
        check=True, capture_output=True,
    )
    return c
