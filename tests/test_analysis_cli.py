"""CLI contracts exercise real source analysis and a temporary SQLite index."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class AnalysisCLITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / 'worker.py').write_text('def persist(value):\n    return value\n', encoding='utf-8')
        (self.root / 'entry.py').write_text('from worker import persist as save\n\ndef submit(value):\n    return save(value)\n', encoding='utf-8')

    def tearDown(self):
        self.tmp.cleanup()

    def call(self, *args, env=None):
        return subprocess.run([sys.executable, '-m', 'paicli', *args], env=env,
                              capture_output=True, text=True, encoding='utf-8', timeout=30)

    def dataset(self):
        data = {'name': 'source-contract', 'version': 1,
                'annotation_note': 'Hand specified test fixture, not model quality evidence.',
                'source_files': {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                                 for p in self.root.glob('*.py')},
                'cases': [
                    {'id': 'save', 'query': 'persist', 'answerable': True, 'category': 'lookup',
                     'relevant_spans': [{'path': 'worker.py', 'start_line': 1, 'end_line': 2}]},
                    {'id': 'missing', 'query': 'unicornquantumtransaction', 'answerable': False,
                     'category': 'unanswerable', 'relevant_spans': []}]}
        path = self.root / 'cases.json'
        path.write_text(json.dumps(data), encoding='utf-8')
        return path

    def test_graph_query_and_neighbor_command_use_current_source_without_state(self):
        result = self.call('graph', '--workspace', str(self.root), '--query', 'persist')
        self.assertEqual(result.returncode, 0, result.stderr)
        symbols = json.loads(result.stdout)['symbols']
        target = next(s for s in symbols if s['path'] == 'worker.py' and s['name'] == 'persist')
        callers = self.call('graph', '--workspace', str(self.root), '--symbol', target['id'], '--direction', 'callers')
        self.assertEqual(callers.returncode, 0, callers.stderr)
        self.assertTrue(any(s['name'] == 'submit' for s in json.loads(callers.stdout)['symbols']))
        self.assertFalse((self.root / '.paicli').exists())
        (self.root / 'worker.py').unlink()
        rebuilt = self.call('graph', '--workspace', str(self.root), '--query', 'persist')
        self.assertEqual(json.loads(rebuilt.stdout)['symbols'], [])

    def test_evaluate_runs_four_methods_and_leaves_workspace_unchanged(self):
        result = self.call('evaluate', str(self.dataset()), '--workspace', str(self.root), '--k', '5')
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(set(data['methods']), {'bm25', 'graph_expand', 'code_aware', 'code_aware_gated'})
        self.assertEqual(data['answers']['status'], 'not_requested')
        self.assertFalse((self.root / '.paicli').exists())

    def test_graph_query_preserves_parse_failure_instead_of_complete_empty_result(self):
        (self.root / 'broken.py').write_text('def sought(\n', encoding='utf-8')
        result = self.call('graph', '--workspace', str(self.root), '--query', 'sought')
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload['symbols'], [])
        self.assertTrue(payload['analysis_incomplete'])
        self.assertTrue(any(d['path'] == 'broken.py' for d in payload['diagnostics']))

    def test_evaluate_refuses_stale_ground_truth(self):
        path = self.dataset()
        (self.root / 'worker.py').write_text('def changed():\n    return False\n', encoding='utf-8')
        result = self.call('evaluate', str(path), '--workspace', str(self.root))
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(json.loads(result.stderr)['type'], 'PolicyError')
        self.assertFalse((self.root / '.paicli').exists())

    def test_explicit_live_evaluation_without_credentials_is_not_a_pass(self):
        env = {k: v for k, v in os.environ.items() if k != 'OPENAI_API_KEY'}
        result = self.call('evaluate', str(self.dataset()), '--workspace', str(self.root),
                           '--provider', 'openai', '--model', 'example-model', env=env)
        self.assertEqual(result.returncode, 1, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(data['answers']['status'], 'not_run')
        self.assertEqual(data['answers']['reason'], 'missing_model_configuration')


if __name__ == '__main__':
    unittest.main()
