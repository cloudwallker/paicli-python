"""Non-streaming OpenAI-compatible adapter and explicit offline scripts."""
from __future__ import annotations

import copy
import json
from urllib.parse import urlsplit

import httpx

from .errors import ModelError


def validate_url(url: str, error_type=ModelError) -> str:
    try:
        parsed = urlsplit(url)
        _ = parsed.port
    except ValueError:
        raise error_type("Invalid HTTP endpoint.") from None
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise error_type("Endpoint must use HTTP(S) without userinfo, query, or fragment.")
    if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise error_type("Remote credentials require HTTPS; HTTP is allowed only for loopback development.")
    return url.rstrip("/")


class ScriptedModel:
    def __init__(self, responses: list[dict]):
        if not isinstance(responses, list):
            raise ModelError("Script must be a JSON array of assistant messages.")
        self.responses = copy.deepcopy(responses)
        self.requests: list[list[dict]] = []

    async def complete(self, messages: list[dict], tools: list[dict]) -> dict:
        self.requests.append(copy.deepcopy(messages))
        if not self.responses:
            raise ModelError("Scripted model exhausted; no implicit fallback is available.")
        return self.responses.pop(0)


class OpenAIModel:
    def __init__(self, base_url: str, model: str, api_key: str, *, client: httpx.AsyncClient | None = None, timeout_seconds: float = 20):
        self.base_url = validate_url(base_url)
        if not isinstance(api_key, str) or not api_key.strip():
            raise ModelError("Set OPENAI_API_KEY in the environment.")
        if not model.strip():
            raise ModelError("A model name is required.")
        self.model = model
        self._api_key = api_key
        self._client = client
        self.timeout_seconds = timeout_seconds

    async def complete(self, messages: list[dict], tools: list[dict]) -> dict:
        payload = {"model": self.model, "messages": messages, "stream": False, "max_tokens": 2048}
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        owned = self._client is None
        client = self._client or httpx.AsyncClient(timeout=self.timeout_seconds, follow_redirects=False)
        try:
            async with client.stream("POST", self.base_url + "/chat/completions", json=payload, headers={"Authorization": "Bearer " + self._api_key}, timeout=self.timeout_seconds, follow_redirects=False) as response:
                if response.status_code < 200 or response.status_code >= 300:
                    raise ModelError(f"Model endpoint returned HTTP {response.status_code}.")
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > 1024 * 1024:
                        raise ModelError("Model response exceeds the 1 MiB limit.")
            data = json.loads(raw)
            message = data["choices"][0]["message"]
            if not isinstance(message, dict) or message.get("role") != "assistant":
                raise ValueError("invalid message")
            return message
        except ModelError:
            raise
        except (httpx.HTTPError, OSError):
            raise ModelError("Model request failed or timed out.") from None
        except (ValueError, KeyError, IndexError, TypeError):
            raise ModelError("Model endpoint returned an invalid chat-completion response.") from None
        finally:
            if owned:
                await client.aclose()
