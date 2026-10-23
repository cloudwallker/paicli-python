"""Read-only platform API integration and DNS/TCP/HTTP diagnostics."""
from __future__ import annotations

import asyncio
import json
import math
import re
import socket
from urllib.parse import urlsplit

import httpx

from .errors import PlatformError
from .models import validate_url


MAX_RESPONSE_BYTES = 1024 * 1024
MAX_EVENTS = 4096
RUN_STATUSES = {"queued", "running", "succeeded", "failed", "interrupted"}
NODE_STATUSES = {"pending", "running", "succeeded", "failed", "interrupted", "skipped"}


def _invalid_response():
    raise PlatformError("Platform returned an invalid response schema.")


def _string(value, *, nullable=False):
    return (nullable and value is None) or (isinstance(value, str) and bool(value))


def _integer(value, minimum=0):
    return type(value) is int and value >= minimum


def _json(raw):
    def nonfinite(_):
        raise ValueError("Non-finite JSON number")
    try:
        return json.loads(raw, parse_constant=nonfinite)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise PlatformError("Platform returned invalid JSON.") from None


def _summary(data):
    if (not isinstance(data, dict) or not _string(data.get("id"))
            or not isinstance(data.get("status"), str) or data["status"] not in RUN_STATUSES):
        _invalid_response()
    for key in ("name", "created_at", "updated_at"):
        if key in data and not _string(data[key]):
            _invalid_response()
    if "error" in data and not (data["error"] is None or isinstance(data["error"], str)):
        _invalid_response()


def _run_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        raise PlatformError("Run ID must contain 1–128 ASCII letters, digits, underscores or dashes.")
    return value


def _events(raw: bytes, after: int) -> dict:
    """Decode bounded, non-following SSE history; reject partial or corrupt replay.

    Body bytes are bounded while streaming, before decoding. A malformed record
    or an exceeded limit fails the whole command; no successful partial history
    or advanced cursor is returned to the caller.
    """
    try:
        text = raw.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
    except UnicodeError:
        raise PlatformError("Platform returned invalid UTF-8 events.") from None
    items, cursor = [], after
    fields, data_lines = {}, []

    def dispatch():
        nonlocal cursor
        if not fields and not data_lines:
            return
        if fields.get("event") == "error":
            # A stream can lose authorization after HTTP 200 has been sent.
            # Never turn its preceding valid records into a successful partial
            # result, and never echo an upstream error detail or credential.
            try:
                control = _json("\n".join(data_lines))
            except PlatformError:
                control = None
            if isinstance(control, dict) and control.get("code") == "authentication_invalid":
                raise PlatformError("Platform authentication expired or was revoked; authenticate again.")
            raise PlatformError("Platform event stream reported an error.")
        if not data_lines or not re.fullmatch(r"[0-9]{1,18}", fields.get("id", "")):
            raise PlatformError("Platform returned an invalid SSE event.")
        item = _json("\n".join(data_lines))
        sequence = int(fields["id"])
        event_type = fields.get("event")
        if (not isinstance(item, dict) or not _integer(item.get("seq"), 1)
                or item["seq"] != sequence or sequence <= cursor
                or not isinstance(event_type, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", event_type)
                or item.get("type") != event_type or not isinstance(item.get("data"), dict)
                or not _string(item.get("created_at"))
                or "node_id" not in item or not _string(item["node_id"], nullable=True)
                or "attempt" not in item or not (item["attempt"] is None or _integer(item["attempt"]))):
            raise PlatformError("Platform returned an invalid or out-of-order SSE event.")
        if len(items) >= MAX_EVENTS:
            raise PlatformError("Platform history exceeds the event limit; query again with --after.")
        items.append(item)
        cursor = sequence

    # split('\n') retains a trailing empty element, but one line ending alone
    # must not dispatch an unfinished event: only an actual empty line does.
    # SSE recognizes CR/LF only. str.splitlines() would also split U+2028 or
    # U+0085 inside perfectly valid JSON strings and corrupt model output.
    lines = text.split("\n")
    for position, line in enumerate(lines):
        terminated = position < len(lines) - 1
        if line == "" and terminated:
            dispatch()
            fields, data_lines = {}, []
        elif not line.startswith(":"):
            name, separator, value = line.partition(":")
            value = value.removeprefix(" ") if separator else ""
            if name == "data":
                data_lines.append(value)
            elif name in {"id", "event"}:
                if name in fields:
                    raise PlatformError("Platform returned duplicate SSE event fields.")
                fields[name] = value
    if fields or data_lines:
        raise PlatformError("Platform returned an incomplete SSE event; retry from the previous cursor.")
    return {"events": items, "cursor": cursor}


class PlatformClient:
    def __init__(self, base_url: str, bearer_token: str, *, client: httpx.AsyncClient | None = None, timeout_seconds: float = 10):
        self.base_url = validate_url(base_url, PlatformError)
        if not isinstance(bearer_token, str) or not bearer_token.strip():
            raise PlatformError("Set FLOWTRAIL_API_TOKEN in the environment.")
        if not all(33 <= ord(character) <= 126 for character in bearer_token):
            raise PlatformError("Platform token must use visible ASCII characters without whitespace.")
        if (isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float))
                or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 60):
            raise PlatformError("Platform timeout must be between 0 and 60 seconds.")
        self._token = bearer_token
        self._client = client
        self.timeout_seconds = timeout_seconds

    async def _download(self, path: str, *, event_cursor: int | None = None) -> bytes:
        owned = self._client is None
        client = self._client or httpx.AsyncClient(timeout=self.timeout_seconds, follow_redirects=False, trust_env=False)
        headers = {"Authorization": "Bearer " + self._token}
        if event_cursor is not None:
            headers.update({"Accept": "text/event-stream", "Last-Event-ID": str(event_cursor)})
        try:
            # httpx's read timeout applies to each chunk. Bound the entire
            # request as well so a slow history stream cannot run indefinitely.
            async with asyncio.timeout(self.timeout_seconds):
                async with client.stream("GET", self.base_url + path, headers=headers, timeout=self.timeout_seconds, follow_redirects=False) as response:
                    if not 200 <= response.status_code < 300:
                        raise PlatformError(f"Platform returned HTTP {response.status_code}.")
                    if event_cursor is not None and response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "text/event-stream":
                        raise PlatformError("Platform event endpoint did not return text/event-stream.")
                    raw = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(raw) + len(chunk) > MAX_RESPONSE_BYTES:
                            raise PlatformError("Platform response exceeds the 1 MiB limit.")
                        raw.extend(chunk)
            return bytes(raw)
        except PlatformError:
            raise
        except (httpx.HTTPError, OSError):
            raise PlatformError("Platform request failed or timed out.") from None
        finally:
            if owned:
                await client.aclose()

    async def runs(self) -> dict:
        data = _json(await self._download("/api/runs"))
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            _invalid_response()
        for item in data["items"]:
            _summary(item)
        return data

    async def me(self) -> dict:
        data = _json(await self._download("/api/me"))
        if (not isinstance(data, dict) or not _string(data.get("tenant_id"))
                or not isinstance(data.get("role"), str) or data["role"] not in {"admin", "operator", "viewer"}):
            _invalid_response()
        return data

    async def quota(self) -> dict:
        data = _json(await self._download("/api/quota"))
        keys = ("used_bytes", "limit_bytes", "remaining_bytes")
        if (not isinstance(data, dict) or not all(_integer(data.get(key)) for key in keys)
                or data["limit_bytes"] < 1 or data["used_bytes"] + data["remaining_bytes"] != data["limit_bytes"]):
            _invalid_response()
        return data

    async def run(self, run_id: str) -> dict:
        data = _json(await self._download("/api/runs/" + _run_id(run_id)))
        _summary(data)
        if (data["id"] != run_id or not isinstance(data.get("workflow"), dict)
                or not _string(data["workflow"].get("name")) or not isinstance(data["workflow"].get("nodes"), list)
                or not isinstance(data.get("inputs"), dict) or not isinstance(data.get("nodes"), list)):
            _invalid_response()
        definition_nodes = data["workflow"]["nodes"]
        if not 1 <= len(definition_nodes) <= 32:
            _invalid_response()
        definitions = {}
        for node in definition_nodes:
            if (not isinstance(node, dict) or not _string(node.get("id"))
                    or node["id"] in definitions or not isinstance(node.get("config"), dict)
                    or not isinstance(node.get("type"), str) or node["type"] not in {"text", "http", "inference"}
                    or not isinstance(node.get("depends_on"), list)
                    or not all(_string(dependency) for dependency in node["depends_on"])):
                _invalid_response()
            definitions[node["id"]] = node["type"]
        seen = set()
        for node in data["nodes"]:
            if (not isinstance(node, dict) or not _string(node.get("id"))
                    or not isinstance(node.get("status"), str) or node["status"] not in NODE_STATUSES
                    or not isinstance(node.get("type"), str) or node["type"] not in {"text", "http", "inference"}
                    or not _integer(node.get("attempt"))):
                _invalid_response()
            if node["id"] in seen or definitions.get(node["id"]) != node["type"]:
                _invalid_response()
            seen.add(node["id"])
            for key in ("output", "error", "started_at", "finished_at"):
                if key not in node or not (node[key] is None or isinstance(node[key], str)):
                    _invalid_response()
        if seen != definitions.keys():
            _invalid_response()
        return data

    async def events(self, run_id: str, *, after: int = 0) -> dict:
        run_id = _run_id(run_id)
        if not _integer(after) or after >= 10 ** 18:
            raise PlatformError("Event cursor must be a nonnegative integer of at most 18 digits.")
        return _events(await self._download("/api/runs/" + run_id + "/events?follow=false", event_cursor=after), after)


async def doctor(url: str, timeout_seconds: float = 3) -> dict:
    """Each stage has its own timeout; HTTP performs a read-only GET, no auth."""
    try:
        parsed = urlsplit(url)
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        raise PlatformError("Invalid diagnostic URL.") from None
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise PlatformError("Diagnostic URL must be HTTP(S), without credentials, query, or fragment.")
    if not 0 < timeout_seconds <= 60:
        raise PlatformError("Diagnostic timeout must be between 0 and 60 seconds.")
    host = parsed.hostname
    result = {"host": host, "port": port, "dns": {"ok": False}, "tcp": {"ok": False}, "http": {"ok": False}}
    try:
        addresses = await asyncio.wait_for(asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM), timeout_seconds)
        result["dns"] = {"ok": True, "addresses": sorted({item[4][0] for item in addresses})}
    except (OSError, TimeoutError):
        result["dns"]["error"] = "DNS resolution failed or timed out."
        result["tcp"]["error"] = "Skipped because DNS failed."
        result["http"]["error"] = "Skipped because DNS failed."
        return result
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout_seconds)
        writer.close()
        await writer.wait_closed()
        result["tcp"] = {"ok": True}
    except (OSError, TimeoutError):
        result["tcp"]["error"] = "TCP connection failed or timed out."
    try:
        async with httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=False, trust_env=False) as client:
            async with client.stream("GET", url) as response:
                result["http"] = {"ok": 200 <= response.status_code < 400, "status": response.status_code}
    except (OSError, httpx.HTTPError):
        result["http"]["error"] = "HTTP request failed or timed out."
    return result
