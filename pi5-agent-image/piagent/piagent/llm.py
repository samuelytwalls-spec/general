"""Minimal client for the local llama.cpp servers (OpenAI-compatible API)."""

from __future__ import annotations

import json
import math
import urllib.error
import urllib.request

from .config import LLMConfig

# The llama.cpp servers listen on loopback; never route them via a proxy.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class LLMError(RuntimeError):
    pass


def _request(url: str, payload: dict | None, timeout: float) -> dict:
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(
        url,
        data=data,
        method="GET" if payload is None else "POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")[:500]
        raise LLMError(f"{url}: HTTP {exc.code}: {body}") from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise LLMError(f"{url}: {exc}") from exc


def l2_normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vec))
    return [v / norm for v in vec] if norm else vec


class LlamaClient:
    def __init__(self, cfg: LLMConfig):
        self.cfg = cfg

    def chat(
        self,
        messages: list[dict],
        json_schema: dict | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> str:
        payload: dict = {
            "model": self.cfg.chat_model,
            "messages": messages,
            "temperature": self.cfg.temperature if temperature is None else temperature,
            "max_tokens": max_tokens or self.cfg.max_tokens,
            "stream": False,
        }
        if json_schema is not None:
            # llama-server compiles the schema to a grammar, so the reply is
            # guaranteed to parse even with a small model.
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "action", "schema": json_schema},
            }
        url = self.cfg.chat_url.rstrip("/") + "/v1/chat/completions"
        resp = _request(url, payload, self.cfg.request_timeout)
        try:
            choice = resp["choices"][0]
            if json_schema is not None and choice.get("finish_reason") == "length":
                # The JSON object was cut off; try once more with a bigger budget.
                payload["max_tokens"] = payload["max_tokens"] * 2
                choice = _request(url, payload, self.cfg.request_timeout)["choices"][0]
            return choice["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"unexpected chat response: {str(resp)[:300]}") from exc

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        resp = _request(
            self.cfg.embed_url.rstrip("/") + "/v1/embeddings",
            {"model": self.cfg.embed_model, "input": texts},
            self.cfg.request_timeout,
        )
        try:
            items = sorted(resp["data"], key=lambda d: d["index"])
            vectors = [l2_normalize([float(x) for x in d["embedding"]]) for d in items]
        except (KeyError, TypeError, ValueError) as exc:
            raise LLMError(f"unexpected embedding response: {str(resp)[:300]}") from exc
        if len(vectors) != len(texts):
            raise LLMError(f"asked for {len(texts)} embeddings, got {len(vectors)}")
        return vectors

    def healthy(self, which: str = "chat") -> bool:
        base = self.cfg.chat_url if which == "chat" else self.cfg.embed_url
        try:
            _request(base.rstrip("/") + "/health", None, 5)
            return True
        except LLMError:
            return False
