from __future__ import annotations

import json
from pathlib import Path
import tempfile
from unittest import IsolatedAsyncioTestCase

import httpx

from paicli.errors import ModelError, PlatformError
from paicli.agent import Agent, validate_reply
from paicli.models import OpenAIModel
from paicli.platform import PlatformClient, doctor
from paicli.storage import Store
from paicli.tools import ToolRegistry
from paicli.workspace import Workspace


class ProtocolTests(IsolatedAsyncioTestCase):
    async def test_final_null_tool_calls_roundtrip_through_adapter_and_agent(self):
        def handler(request):
            return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "Done", "tool_calls": None}}]})
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Workspace(Path(temporary))
            store = Store(workspace)
            try:
                async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                    model = OpenAIModel("https://model.example/v1", "test", "placeholder", client=client)
                    result = await Agent(model, ToolRegistry(workspace, store=store), store).run("Inspect", session="null-final")
                self.assertEqual(result["answer"], "Done")
                self.assertEqual(result["tool_calls"], 0)
                self.assertEqual(store.messages("null-final")[-1], {"role": "assistant", "content": "Done"})
                for malformed in ({}, "", False):
                    with self.subTest(tool_calls=malformed), self.assertRaises(ModelError):
                        validate_reply({"role": "assistant", "content": "Done", "tool_calls": malformed}, max_calls=16)
            finally:
                store.close()

    async def test_openai_request_and_tool_call_response(self):
        seen = []
        def handler(request):
            seen.append(request)
            return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [{"id": "c", "type": "function", "function": {"name": "list_files", "arguments": "{}"}}]}}]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            model = OpenAIModel("https://model.example/v1", "test-model", "test-placeholder", client=client)
            result = await model.complete([{"role": "user", "content": "list"}], [])
        self.assertEqual(str(seen[0].url), "https://model.example/v1/chat/completions")
        self.assertEqual(seen[0].headers["authorization"], "Bearer test-placeholder")
        self.assertFalse(json.loads(seen[0].content)["stream"])
        self.assertEqual(result["tool_calls"][0]["id"], "c")

    async def test_model_http_failure_does_not_expose_response_or_key(self):
        def handler(request):
            return httpx.Response(401, text="server-body-must-not-leak")
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            model = OpenAIModel("https://model.example/v1", "test", "key-must-not-leak", client=client)
            with self.assertRaises(ModelError) as caught:
                await model.complete([], [])
        self.assertNotIn("must-not-leak", str(caught.exception))
        self.assertIn("401", str(caught.exception))

    async def test_malformed_model_response_rejected(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"unexpected": 1}))) as client:
            with self.assertRaises(ModelError):
                await OpenAIModel("https://model.example/v1", "test", "placeholder", client=client).complete([], [])

    async def test_platform_runs_protocol(self):
        seen = []
        def handler(request):
            seen.append(request)
            return httpx.Response(200, json={"items": [{"id": "run1", "status": "succeeded"}]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await PlatformClient("http://127.0.0.1:8000", "example-token", client=client).runs()
        self.assertEqual(str(seen[0].url), "http://127.0.0.1:8000/api/runs")
        self.assertEqual(seen[0].headers["authorization"], "Bearer example-token")
        self.assertEqual(result["items"][0]["id"], "run1")

    async def test_platform_error_omits_response(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(403, text="secret-body"))) as client:
            with self.assertRaises(PlatformError) as caught:
                await PlatformClient("http://127.0.0.1:8000", "placeholder", client=client).runs()
        self.assertNotIn("secret-body", str(caught.exception))

    async def test_remote_plaintext_credentials_rejected(self):
        with self.assertRaises(PlatformError):
            PlatformClient("http://platform.example", "placeholder")
        with self.assertRaises(ModelError):
            OpenAIModel("http://model.example/v1", "model", "placeholder")

    async def test_doctor_loopback_dns_tcp_http(self):
        import asyncio
        async def respond(reader, writer):
            try:
                await reader.readuntil(b"\r\n\r\n")
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nOK")
                await writer.drain()
            except asyncio.IncompleteReadError:
                pass  # The independent TCP probe connects without an HTTP request.
            finally:
                writer.close()
                await writer.wait_closed()
        server = await asyncio.start_server(respond, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        async with server:
            result = await doctor(f"http://127.0.0.1:{port}/", timeout_seconds=1)
        self.assertTrue(result["dns"]["ok"])
        self.assertTrue(result["tcp"]["ok"])
        self.assertTrue(result["http"]["ok"])
