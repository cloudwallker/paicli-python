"""Runnable miniature implementations show behavior words are not denylisted."""
from __future__ import annotations

from pathlib import Path
import runpy
import tempfile
import threading
import unittest

from paicli.index import CodeIndex
from paicli.retrieval import CodeRetriever
from paicli.storage import Store
from paicli.workspace import Workspace


class RunnableBehaviorExamples(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.workspace = Workspace(self.root)
        self.store = Store(self.workspace)
        self.addCleanup(self.store.close)
        self.index = CodeIndex(self.workspace, self.store)

    def implementation(self, source):
        path = self.root / 'example.py'
        path.write_text(source, encoding='utf-8')
        self.index.build()
        return runpy.run_path(str(path)), CodeRetriever(self.index)

    def test_checkpoint_roundtrip_is_retrievable_after_real_disk_persistence(self):
        code, retriever = self.implementation(
            'import json\nfrom pathlib import Path\n'
            'def save_checkpoint(path, state):\n'
            '    Path(path).write_text(json.dumps(state), encoding="utf-8")\n'
            'def restore_checkpoint(path):\n'
            '    return json.loads(Path(path).read_text(encoding="utf-8"))\n')
        path = self.root / 'checkpoint.json'
        code['save_checkpoint'](path, {'completed': 2})
        self.assertEqual(code['restore_checkpoint'](path), {'completed': 2})
        result = retriever.search('检查点')
        self.assertEqual(result['decision']['status'], 'accepted')
        self.assertTrue(any('restore_checkpoint' in hit['content'] for hit in result['hits']))

    def test_expiration_cleanup_is_retrievable_after_filtering_expired_messages(self):
        code, retriever = self.implementation(
            'def cleanup_expired_messages(history, now):\n'
            '    return [message for message in history if message["expires_at"] > now]\n')
        history = [{'text': 'old', 'expires_at': 9}, {'text': 'keep', 'expires_at': 11}]
        self.assertEqual(code['cleanup_expired_messages'](history, 10), [history[1]])
        result = retriever.search('expiration cleanup message history')
        self.assertEqual(result['decision']['status'], 'accepted')
        self.assertEqual(result['hits'][0]['missing_behaviors'], [])

    def test_background_file_watcher_is_retrievable_after_observing_a_change(self):
        code, retriever = self.implementation(
            'from pathlib import Path\nfrom threading import Thread\n'
            'def watch_file_in_background(path, changed, ready, stop):\n'
            '    def poll_file():\n'
            '        previous = Path(path).read_bytes()\n'
            '        ready.set()\n'
            '        while not stop.wait(0.005):\n'
            '            current = Path(path).read_bytes()\n'
            '            if current != previous:\n'
            '                changed.set()\n'
            '                return\n'
            '    worker = Thread(target=poll_file, daemon=True)\n'
            '    worker.start()\n'
            '    return worker\n')
        path = self.root / 'watched.txt'
        path.write_text('before', encoding='utf-8')
        changed, ready, stop = threading.Event(), threading.Event(), threading.Event()
        worker = code['watch_file_in_background'](path, changed, ready, stop)
        try:
            self.assertTrue(ready.wait(2), 'watcher did not start')
            path.write_text('after', encoding='utf-8')
            self.assertTrue(changed.wait(2), 'watcher did not observe file change')
        finally:
            stop.set()
            worker.join(2)
        self.assertFalse(worker.is_alive())
        result = retriever.search('background watch file')
        self.assertEqual(result['decision']['status'], 'accepted')
        self.assertEqual(result['hits'][0]['missing_behaviors'], [])


if __name__ == '__main__':
    unittest.main()
