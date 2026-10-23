from __future__ import annotations

import asyncio
import json
from pathlib import Path
import tempfile
import unittest

from paicli.agent import Agent
from paicli.errors import PolicyError
from paicli.models import ScriptedModel
from paicli.storage import Store
from paicli.tools import ToolRegistry
from paicli.workspace import Workspace


class GraphToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / 'ledger.py').write_text('def commit(value):\n    return value\n', encoding='utf-8')
        (self.root / 'api.py').write_text('from ledger import commit\n\ndef submit(value):\n    return commit(value)\n', encoding='utf-8')
        self.workspace = Workspace(self.root)
        self.tools = ToolRegistry(self.workspace, read_only=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_read_only_tools_find_definition_callers_and_callees(self):
        found = self.tools.execute('find_symbol', {'query': 'commit'})['symbols']
        symbol = next(s for s in found if s['name'] == 'commit')
        related = self.tools.execute('callers', {'symbol_id': symbol['id']})
        submit = next(s for s in related['symbols'] if s['name'] == 'submit')
        callees = self.tools.execute('callees', {'symbol_id': submit['id']})
        self.assertIn(symbol['id'], {s['id'] for s in callees['symbols']})
        self.assertFalse((self.root / '.paicli').exists())

    def test_changed_source_does_not_reuse_old_graph(self):
        found = self.tools.execute('find_symbol', {'query': 'commit'})['symbols']
        self.assertTrue(found)
        (self.root / 'ledger.py').write_text('def replaced():\n    return None\n', encoding='utf-8')
        self.assertEqual(self.tools.execute('find_symbol', {'query': 'commit'})['symbols'], [])

    def test_graph_tools_preserve_incomplete_analysis_diagnostics(self):
        (self.root / 'broken.py').write_text('def sought(\n', encoding='utf-8')
        found = self.tools.execute('find_symbol', {'query': 'commit'})
        self.assertTrue(found['analysis_incomplete'])
        self.assertTrue(any(d['path'] == 'broken.py' for d in found['diagnostics']))
        related = self.tools.execute('callers', {'symbol_id': found['symbols'][0]['id']})
        self.assertTrue(related['analysis_incomplete'])
        self.assertEqual(related['diagnostics'], found['diagnostics'])

    def test_graph_tools_reject_bad_parameters_and_readonly_write(self):
        # Exercise a valid call first so absence of the tool cannot satisfy this test.
        self.tools.execute('find_symbol', {'query': 'submit'})
        for name, args in [('callers', {'symbol_id': 'x', 'depth': 9}),
                           ('callees', {'symbol_id': 'x', 'limit': 0}),
                           ('find_symbol', {'query': 'x', 'limit': True}),
                           ('find_symbol', {'query': 'x', 'untrusted': 'yes'}),
                           ('write_file', {'path': 'x.txt', 'content': 'bad'})]:
            with self.subTest(name=name), self.assertRaises(PolicyError):
                self.tools.execute(name, args)

    def test_agent_roundtrip_records_graph_tool_result(self):
        model = ScriptedModel([
            {'role': 'assistant', 'content': None, 'tool_calls': [
                {'id': 'graph1', 'type': 'function', 'function': {
                    'name': 'find_symbol', 'arguments': json.dumps({'query': 'commit'})}}]},
            {'role': 'assistant', 'content': 'Source was inspected using an explicit offline script.'}])
        store = Store(self.workspace)
        try:
            result = asyncio.run(Agent(model, ToolRegistry(self.workspace, store=store, read_only=True), store).run('Locate persistence'))
            self.assertEqual(result['trace'], [{'tool': 'find_symbol', 'ok': True}])
            recorded = next(m for m in model.requests[1] if m['role'] == 'tool')
            payload = json.loads(recorded['content'])
            self.assertTrue(any(s['name'] == 'commit' for s in payload['result']['symbols']))
        finally:
            store.close()
