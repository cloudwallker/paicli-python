"""Read-only platform API integration and DNS/TCP/HTTP diagnostics."""
from __future__ import annotations

import asyncio
import json
import socket
from urllib.parse import urlsplit

import httpx

from .errors import PlatformError
from .models import validate_url


class PlatformClient:
    def __init__(self, base_url: str, bearer_token: str, *, client: httpx.AsyncClient | None = None, timeout_seconds: float = 10):
        self.base_url = validate_url(base_url, PlatformError)
        if not isinstance(bearer_token, str) or not bearer_token.strip():
            raise PlatformError("Set FLOWTRAIL_API_TOKEN in the environment.")
        self._token = bearer_token
        self._client = client
        self.timeout_seconds = timeout_seconds

    async def runs(self) -> dict | list:
        owned = self._client is None
        client = self._client or httpx.AsyncClient(timeout=self.timeout_seconds, follow_redirects=False)
        try:
            async with client.stream("GET", self.base_url + "/api/runs", headers={"Authorization": "Bearer " + self._token}, timeout=self.timeout_seconds, follow_redirects=False) as response:
                if not 200 <= response.status_code < 300:
                    raise PlatformError(f"Platform returned HTTP {response.status_code}.")
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > 1024 * 1024:
                        raise PlatformError("Platform response exceeds the 1 MiB limit.")
            data = json.loads(raw)
            if not isinstance(data, (dict, list)):
                raise ValueError("invalid response")
            return data
        except PlatformError:
            raise
        except (httpx.HTTPError, OSError):
            raise PlatformError("Platform request failed or timed out.") from None
        except (ValueError, TypeError):
            raise PlatformError("Platform returned invalid JSON.") from None
        finally:
            if owned:
                await client.aclose()


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
