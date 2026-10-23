"""Real CLI and Agent contracts for evidence-gated bilingual retrieval."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from paicli.agent import Agent
from paicli.errors import BudgetExceeded, PolicyError
from paicli.index import CodeIndex
from paicli.models import ScriptedModel
from paicli.storage import Store
from paicli.tools import ToolRegistry
from paicli.workspace import Workspace


class RetrievalContractTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / 'history.py').write_text(
            'def compact_context(history, budget):\n'
            '    """Compact conversation context and history within a budget."""\n'
            '    return history[-budget:]\n', encoding='utf-8')
        self.workspace = Workspace(self.root)
        self.store = Store(self.workspace)
        self.addCleanup(self.store.close)
        CodeIndex(self.workspace, self.store).build()
        self.tools = ToolRegistry(self.workspace, store=self.store, read_only=True)

    def cli(self, query):
        return subprocess.run([sys.executable, '-m', 'paicli', 'retrieve', query,
                               '--workspace', str(self.root)], capture_output=True,
                              text=True, encoding='utf-8', timeout=20)

    def test_cli_chinese_query_returns_visible_source_and_no_raw_candidates(self):
        result = self.cli('压缩上下文')
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload['decision']['status'], 'accepted')
        self.assertTrue(any('def compact_context' in hit['content'] for hit in payload['hits']))
        self.assertNotIn('candidates', payload)

    def test_cli_insufficient_evidence_is_explicit_without_rejected_source(self):
        result = self.cli('QuantumNimbus network protocol')
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload['decision']['status'], 'insufficient_evidence')
        self.assertEqual(payload['hits'], [])
        self.assertNotIn('candidates', payload)
        self.assertNotIn('return history', result.stdout)

    def test_readonly_agent_roundtrip_preserves_evidence_decision(self):
        model = ScriptedModel([
            {'role': 'assistant', 'content': None, 'tool_calls': [
                {'id': 'retrieve1', 'type': 'function', 'function': {
                    'name': 'retrieve_code', 'arguments': json.dumps({'query': '压缩上下文'})}}]},
            {'role': 'assistant', 'content': 'Explicit scripted fixture, not a model quality result.'},
        ])
        result = asyncio.run(Agent(model, self.tools, self.store).run('Locate context handling'))
        self.assertEqual(result['trace'], [{'tool': 'retrieve_code', 'ok': True}])
        response = next(m for m in model.requests[1] if m['role'] == 'tool')
        payload = json.loads(response['content'])['result']
        self.assertEqual(payload['decision']['status'], 'accepted')
        self.assertNotIn('candidates', payload)
        self.assertTrue(payload['hits'])

    def test_tool_validation_and_deadline_do_not_weaken_readonly_policy(self):
        self.tools.execute('retrieve_code', {'query': 'context'})
        for arguments in ({'query': 'x', 'limit': True}, {'query': 'x', 'limit': 0},
                          {'query': 'x', 'limit': 21}, {'query': 'x', 'threshold': 0}, {'query': ''}):
            with self.subTest(arguments=arguments), self.assertRaises(PolicyError):
                self.tools.execute('retrieve_code', arguments)
        with self.assertRaises(BudgetExceeded):
            self.tools.execute('retrieve_code', {'query': 'context'}, deadline=0)
        with self.assertRaises(PolicyError):
            self.tools.execute('write_file', {'path': 'history.py', 'content': 'mutated'})


if __name__ == '__main__':
    unittest.main()
