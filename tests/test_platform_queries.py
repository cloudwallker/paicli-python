from __future__ import annotations

import asyncio
import json
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch

import httpx

from paicli.errors import PlatformError
from paicli.platform import PlatformClient


STAMP = "2026-10-23T00:00:00.000+00:00"
RUN = {"id": "run1", "name": "demo", "status": "succeeded", "created_at": STAMP,
       "updated_at": STAMP, "error": None, "inputs": {},
       "workflow": {"name": "demo", "nodes": [{"id": "hello", "type": "text", "depends_on": [], "config": {"template": "hello"}}]},
       "nodes": [{"id": "hello", "type": "text", "status": "succeeded", "attempt": 1,
                  "output": "hello", "error": None, "started_at": STAMP, "finished_at": STAMP}]}


def event(sequence, event_type="RUN_SUCCEEDED"):
    return {"seq": sequence, "type": event_type, "node_id": None, "attempt": None, "data": {}, "created_at": STAMP}


def frame(sequence, event_type="RUN_SUCCEEDED"):
    return f"id: {sequence}\nevent: {event_type}\ndata: {json.dumps(event(sequence, event_type))}\n\n"


class Parts(httpx.AsyncByteStream):
    def __init__(self, parts):
        self.parts = parts

    async def __aiter__(self):
        for part in self.parts:
            yield part


class PlatformQueryTests(IsolatedAsyncioTestCase):
    async def test_identity_quota_and_run_use_real_endpoint_shapes(self):
        responses = {
            "/api/me": {"tenant_id": "demo", "role": "viewer", "token_id": "a" * 32, "expires_at": STAMP},
            "/api/quota": {"used_bytes": 3, "limit_bytes": 100, "remaining_bytes": 97},
            "/api/runs/run1": RUN,
        }
        seen = []
        def handler(request):
            seen.append(request)
            return httpx.Response(200, json=responses[request.url.path])
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = PlatformClient("http://127.0.0.1:8000", "example-token", client=http)
            self.assertEqual((await client.me())["role"], "viewer")
            self.assertEqual((await client.quota())["remaining_bytes"], 97)
            self.assertEqual((await client.run("run1"))["nodes"][0]["output"], "hello")
        self.assertEqual([r.url.path for r in seen], ["/api/me", "/api/quota", "/api/runs/run1"])
        self.assertTrue(all(r.headers["authorization"] == "Bearer example-token" for r in seen))

    async def test_schema_errors_are_rejected_without_echoing_payload(self):
        invalid = [
            ("runs", [], {"items": [{"id": "run1", "status": "invented"}]}),
            ("runs", [], {"items": "secret-payload"}),
            ("me", [], {"tenant_id": "demo", "role": "root", "private": "secret-payload"}),
            ("me", [], {"tenant_id": 42, "role": "viewer"}),
            ("quota", [], {"used_bytes": True, "limit_bytes": 100, "remaining_bytes": 99}),
            ("quota", [], {"used_bytes": 3, "limit_bytes": 100, "remaining_bytes": 100}),
            ("run", ["run1"], {**RUN, "nodes": "secret-payload"}),
            ("run", ["run1"], {**RUN, "id": "other"}),
            ("run", ["run1"], {**RUN, "nodes": [{**RUN["nodes"][0], "attempt": -1}]}),
            ("run", ["run1"], {**RUN, "workflow": {"name": "demo", "nodes": ["invalid-node"]}}),
            ("run", ["run1"], {**RUN, "nodes": [{**RUN["nodes"][0], "id": "different"}]}),
            ("run", ["run1"], {**RUN, "nodes": [{**RUN["nodes"][0], "status": "queued"}]}),
        ]
        for method, args, payload in invalid:
            with self.subTest(method=method, payload=payload):
                async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload))) as http:
                    client = PlatformClient("http://127.0.0.1:8000", "private-token", client=http)
                    with self.assertRaises(PlatformError) as caught:
                        await getattr(client, method)(*args)
                    self.assertNotIn("secret-payload", str(caught.exception))
                    self.assertNotIn("private-token", str(caught.exception))

    async def test_http_errors_do_not_echo_token_response_or_redirect(self):
        for code in (301, 401, 403, 404):
            with self.subTest(code=code):
                seen = []
                def handler(request):
                    seen.append(request)
                    return httpx.Response(code, text="private-token secret-payload", headers={"Location": "https://elsewhere.example/api/me"})
                async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
                    with self.assertRaises(PlatformError) as caught:
                        await PlatformClient("https://platform.example", "private-token", client=http).me()
                self.assertIn(str(code), str(caught.exception))
                self.assertNotIn("private-token", str(caught.exception))
                self.assertNotIn("secret-payload", str(caught.exception))
                self.assertEqual(len(seen), 1)

    async def test_response_limit_is_explicit_instead_of_partial_success(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, stream=Parts([b" " * (512 * 1024), b" " * (512 * 1024), b"x"])))) as http:
            with self.assertRaisesRegex(PlatformError, "1 MiB"):
                await PlatformClient("http://127.0.0.1", "example-token", client=http).runs()

    async def test_non_json_and_non_finite_json_are_rejected(self):
        for raw in (b"not-json", b'{"items": [], "unexpected": NaN}'):
            with self.subTest(raw=raw):
                async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=raw))) as http:
                    with self.assertRaises(PlatformError):
                        await PlatformClient("http://127.0.0.1", "example-token", client=http).runs()

    async def test_invalid_run_ids_and_cursors_fail_before_network(self):
        seen = []
        def handler(request):
            seen.append(request)
            return httpx.Response(500)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = PlatformClient("http://127.0.0.1", "example-token", client=http)
            for run_id in ("../secret", "bad?x=1", "id/events", "", "x" * 129):
                with self.subTest(run_id=run_id), self.assertRaises(PlatformError):
                    await client.run(run_id)
            for cursor in (-1, True, "2\r\nAuthorization: other", 10 ** 18):
                with self.subTest(cursor=cursor), self.assertRaises(PlatformError):
                    await client.events("run1", after=cursor)
        self.assertEqual(seen, [])

    async def test_events_parse_multiline_json_comments_crlf_and_replay_cursor(self):
        payload = ": heartbeat\r\n\r\n" + frame(3, "RUN_STARTED").replace("\n", "\r\n")
        # SSE multiline data is joined by a newline before JSON decoding.
        payload += 'id: 4\nevent: RUN_SUCCEEDED\ndata: {"seq":4,"type":"RUN_SUCCEEDED",\ndata: "node_id":null,"attempt":null,"data":{"text":"中文"},"created_at":"' + STAMP + '"}\n\n'
        raw = payload.encode("utf-8")
        seen = []
        def handler(request):
            seen.append(request)
            return httpx.Response(200, headers={"Content-Type": "text/event-stream; charset=utf-8"}, stream=Parts([raw[i:i + 7] for i in range(0, len(raw), 7)]))
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            result = await PlatformClient("http://127.0.0.1:8000", "example-token", client=http).events("run1", after=2)
        self.assertEqual(result["cursor"], 4)
        self.assertEqual([item["seq"] for item in result["events"]], [3, 4])
        self.assertEqual(result["events"][1]["data"]["text"], "中文")
        self.assertEqual(str(seen[0].url), "http://127.0.0.1:8000/api/runs/run1/events?follow=false")
        self.assertEqual(seen[0].headers["Last-Event-ID"], "2")
        self.assertEqual(seen[0].headers["Accept"], "text/event-stream")

    async def test_empty_events_preserve_requested_cursor(self):
        for payload in (b"", b": keepalive\n\n"):
            with self.subTest(payload=payload):
                async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=payload, headers={"Content-Type": "text/event-stream"}))) as http:
                    result = await PlatformClient("http://127.0.0.1", "example-token", client=http).events("run1", after=12)
                self.assertEqual(result, {"events": [], "cursor": 12})

    async def test_sse_preserves_unicode_separators_inside_json_strings(self):
        item = {**event(1), "data": {"text": "before\u2028after\u0085end"}}
        payload = "id: 1\nevent: RUN_SUCCEEDED\ndata: " + json.dumps(item, ensure_ascii=False) + "\n\n"
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=payload, headers={"Content-Type": "text/event-stream"}))) as http:
            result = await PlatformClient("http://127.0.0.1", "example-token", client=http).events("run1")
        self.assertEqual(result["events"][0]["data"]["text"], "before\u2028after\u0085end")

    async def test_malformed_incomplete_and_out_of_order_events_are_not_silently_dropped(self):
        malformed = [
            "id: 1\nevent: RUN_STARTED\ndata: not-json\n\n",
            frame(1).replace('"seq": 1', '"seq": 2'),
            frame(1).replace("event: RUN_SUCCEEDED", "event: RUN_STARTED"),
            frame(1) + frame(1),
            frame(2) + frame(1),
            frame(1).rstrip("\n"),
            "id: 1\n\n",
            "data: {}\n\n",
            frame(1).replace('"data": {}', '"data": []'),
            frame(1).replace("id: 1", "id: private-token"),
        ]
        for payload in malformed:
            with self.subTest(payload=payload):
                async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=payload, headers={"Content-Type": "text/event-stream"}))) as http:
                    with self.assertRaises(PlatformError) as caught:
                        await PlatformClient("http://127.0.0.1", "private-token", client=http).events("run1")
                    self.assertNotIn("private-token", str(caught.exception))

    async def test_event_count_limit_is_reported_without_truncation(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=frame(1) + frame(2) + frame(3), headers={"Content-Type": "text/event-stream"}))) as http:
            with patch("paicli.platform.MAX_EVENTS", 2), self.assertRaisesRegex(PlatformError, "event limit"):
                await PlatformClient("http://127.0.0.1", "example-token", client=http).events("run1")

    async def test_events_require_sse_content_type_and_bounded_bytes(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"error": "unexpected"}))) as http:
            with self.assertRaises(PlatformError):
                await PlatformClient("http://127.0.0.1", "example-token", client=http).events("run1")
        raw = b": " + b"x" * (1024 * 1024)
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=raw, headers={"Content-Type": "text/event-stream"}))) as http:
            with self.assertRaisesRegex(PlatformError, "1 MiB"):
                await PlatformClient("http://127.0.0.1", "example-token", client=http).events("run1")

    async def test_slow_stream_hits_total_deadline_without_partial_event_success(self):
        class SlowParts(httpx.AsyncByteStream):
            async def __aiter__(self):
                for number in range(1, 4):
                    await asyncio.sleep(0.03)
                    yield frame(number).encode()
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, stream=SlowParts(), headers={"Content-Type": "text/event-stream"}))) as http:
            with self.assertRaisesRegex(PlatformError, "timed out"):
                await PlatformClient("http://127.0.0.1", "example-token", client=http, timeout_seconds=0.05).events("run1")

    async def test_authentication_control_event_after_full_batch_rejects_partial_history(self):
        payload = "".join(frame(number) for number in range(1, 513))
        payload += 'event: error\ndata: {"code":"authentication_invalid"}\n\n'
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=payload, headers={"Content-Type": "text/event-stream"}))) as http:
            with self.assertRaisesRegex(PlatformError, "authentication expired or was revoked"):
                await PlatformClient("http://127.0.0.1", "example-token", client=http).events("run1")

    async def test_unknown_or_malformed_error_control_events_fail_without_echoing_details(self):
        for error in [{"code": "unknown", "detail": "private-token"}, [], {"private-token": "secret-body"}]:
            with self.subTest(error=error):
                payload = frame(1) + "event: error\ndata: " + json.dumps(error) + "\n\n"
                async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=payload, headers={"Content-Type": "text/event-stream"}))) as http:
                    with self.assertRaisesRegex(PlatformError, "event stream reported an error") as caught:
                        await PlatformClient("http://127.0.0.1", "private-token", client=http).events("run1")
                    self.assertNotIn("private-token", str(caught.exception))
                    self.assertNotIn("secret-body", str(caught.exception))
