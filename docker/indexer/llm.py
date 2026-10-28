"""OpenAI-compatible chat call shared by Ask and the topic survey.

Same pattern as insights_api._call_llm: `stream: true` is deliberate, because
a buffered (non-streamed) response from the LAN LLM is blackholed by the
container NAT return path while streamed chunks get through. SSE deltas are
accumulated into the full answer.
"""
from __future__ import annotations

import json
import logging
from typing import Optional

import httpx

logger = logging.getLogger("indexer.llm")


class LLMUnavailable(Exception):
    """Not configured or unreachable (HTTP 503)."""


class LLMError(Exception):
    """The LLM answered with an error or nothing (HTTP 502)."""


class LLM:
    def __init__(self, base_url: str, model: str, api_key: str = "", timeout: float = 180.0,
                 transport: Optional[httpx.BaseTransport] = None):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self._transport = transport

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.model)

    def chat(self, system: str, user: str, temperature: float = 0.2) -> str:
        if not self.configured:
            raise LLMUnavailable("LLM not configured. Set LLM_BASE_URL and LLM_MODEL.")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": temperature,
            "stream": True,
        }
        pieces: list[str] = []
        try:
            with httpx.Client(timeout=self.timeout, transport=self._transport) as client:
                with client.stream("POST", f"{self.base_url}/chat/completions",
                                   json=payload, headers=headers) as r:
                    if r.status_code >= 400:
                        body = r.read().decode("utf-8", "replace")[:200]
                        raise LLMError(f"LLM error {r.status_code}: {body}")
                    for line in r.iter_lines():
                        if not line or not line.startswith("data:"):
                            continue
                        data = line[len("data:"):].strip()
                        if data == "[DONE]":
                            break
                        try:
                            delta = json.loads(data)["choices"][0].get("delta") or {}
                        except (json.JSONDecodeError, KeyError, IndexError, AttributeError):
                            continue
                        if delta.get("content"):
                            pieces.append(delta["content"])
        except httpx.HTTPError as e:
            raise LLMUnavailable(f"LLM unreachable at {self.base_url}: {e}") from e
        answer = "".join(pieces).strip()
        if not answer:
            raise LLMError("LLM returned an empty response.")
        return answer
