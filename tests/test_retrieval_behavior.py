from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from paicli.index import CodeIndex
from paicli.storage import Store
from paicli.workspace import Workspace


class BehaviorRetrievalTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.workspace = Workspace(self.root)
        self.store = Store(self.workspace)
        self.addCleanup(self.store.close)
        self.index = CodeIndex(self.workspace, self.store)

    def retriever(self, content, name="workflow.py"):
        from paicli.retrieval import CodeRetriever

        (self.root / name).write_text(content, encoding="utf-8")
        self.index.build()
        return CodeRetriever(self.index)

    def test_specific_behaviors_reject_generic_operations_and_accept_real_identifiers(self):
        cases = [
            (
                ["发生错误时如何验证检查点并继续之前的任务执行？",
                 "How does task validation recover from errors by resuming a checkpoint?"],
                "def validate_task_error(value):\n    return value\n",
                "def validate_task_error(value):\n    return restore_checkpoint(value)\n",
                ["checkpoint"],
            ),
            (
                ["历史消息如何在保留七天后过期并自动清理？",
                 "How does message history expire after seven days and clean up old entries?"],
                "def message_history(value):\n    return value\n",
                "def message_history(value):\n    return cleanup_expired(value)\n",
                ["expiration", "cleanup"],
            ),
            (
                ["文件索引如何在后台监视变更并更新？",
                 "How does the file index watch changes in the background and update results?"],
                "def file_index_update(value):\n    return value\n",
                "def file_index_update(value):\n    return background_watch(value)\n",
                ["watch", "background"],
            ),
        ]
        for queries, generic, implemented, required in cases:
            for query in queries:
                with self.subTest(query=query):
                    absent = self.retriever(generic).search(query)
                    self.assertTrue(absent["candidates"])
                    self.assertEqual(absent["hits"], [])
                    self.assertEqual(set(absent["query"]["required_behaviors"]), set(required))
                    self.assertEqual(set(absent["decision"]["missing_behaviors"]), set(required))
                    present = self.retriever(implemented).search(query)
                    self.assertEqual(present["decision"]["status"], "accepted")
                    self.assertEqual(set(present["hits"][0]["matched_behaviors"]), set(required))
                    self.assertTrue(present["hits"][0]["behavior_evidence"])

    def test_behavior_aliases_match_snake_camel_and_operational_keywords(self):
        cases = [
            ("checkpoint", "def run(value):\n    return restoreSavepoint(value)\n", "checkpoint"),
            ("断点", "def run(value):\n    return restore_checkpoint(value)\n", "checkpoint"),
            ("过期", "def run(value):\n    return value.ttl\n", "expiration"),
            ("到期", "def run(value):\n    return value.expires\n", "expiration"),
            ("retention", "def run(value):\n    return value.expiration\n", "expiration"),
            ("清除", "def run(value):\n    return purge(value)\n", "cleanup"),
            ("cleanup", "def run(value):\n    return clean_up(value)\n", "cleanup"),
            ("清理", "def run(value):\n    return gc.collect(value)\n", "cleanup"),
            ("监视", "def run(value):\n    return value.observer()\n", "watch"),
            ("watching", "def run(value):\n    return poll(value)\n", "watch"),
            ("监控", "def run(value):\n    return monitor(value)\n", "watch"),
            ("后台", "def run(value):\n    return Thread(value, daemon=True)\n", "background"),
        ]
        for query, source, behavior in cases:
            with self.subTest(query=query):
                result = self.retriever(source).search(query)
                self.assertEqual(result["decision"]["status"], "accepted")
                self.assertEqual(result["query"]["required_behaviors"], [behavior])
                evidence = result["hits"][0]["behavior_evidence"]
                self.assertTrue(all(item["identifier"] and item["line"] >= 1 and item["scope"] and item["kind"] for item in evidence))

    def test_longest_chinese_match_keeps_checkpoint_out_of_generic_validation(self):
        result = self.retriever("def validate(value):\n    return value\n").search("检查点")
        self.assertEqual(result["hits"], [])
        self.assertEqual(result["query"]["required_behaviors"], ["checkpoint"])
        self.assertNotIn("validation", [item["term"] for item in result["query"]["concepts"]])

    def test_comments_docstrings_strings_imports_and_stubs_do_not_prove_behavior(self):
        fakes = [
            "# checkpoint expiration cleanup watch background\ndef ordinary(value):\n    return value\n",
            '"""checkpoint expiration cleanup watch background"""\ndef ordinary(value):\n    return value\n',
            'def ordinary(value):\n    """checkpoint expiration cleanup watch background"""\n    return value\n',
            'def ordinary(value):\n    return "checkpoint expiration cleanup watch background"\n',
            "from components import checkpoint, expiration, cleanup, watch, background\ndef ordinary(value):\n    return value\n",
            "def checkpoint_expiration_cleanup_watch_background():\n    pass\n",
            "async def checkpoint_expiration_cleanup_watch_background():\n    ...\n",
            "def checkpoint_expiration_cleanup_watch_background():\n    raise NotImplementedError()\n",
            "def checkpoint_expiration_cleanup_watch_background():\n    return NotImplemented\n",
            "def checkpoint_expiration_cleanup_watch_background():\n    return None\n",
            "def checkpoint_expiration_cleanup_watch_background():\n    return ...\n",
            "class CheckpointExpirationCleanupWatchBackground:\n    pass\n",
            "def ordinary(checkpoint_expiration_cleanup_watch_background):\n    return 1\n",
            "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    checkpoint_expiration_cleanup_watch_background()\n",
            "from typing import TYPE_CHECKING\nif TYPE_CHECKING and ready:\n    checkpoint_expiration_cleanup_watch_background()\n",
            "value: checkpoint_expiration_cleanup_watch_background\n",
        ]
        for source in fakes:
            with self.subTest(source=source):
                result = self.retriever(source).search("checkpoint expiration cleanup watch background")
                self.assertTrue(result["candidates"])
                self.assertEqual(result["hits"], [])
                self.assertEqual(result["decision"]["reason"], "unsupported_required_behaviors")

    def test_import_followed_by_actual_call_is_auditable_evidence(self):
        source = "from helpers import cleanup_expired\ndef message_history(value):\n    return cleanup_expired(value)\n"
        result = self.retriever(source).search("expiration cleanup")
        self.assertEqual(result["decision"]["status"], "accepted")
        self.assertTrue(all(item["line"] == 3 for item in result["hits"][0]["behavior_evidence"]))

    def test_background_false_keyword_does_not_supply_background_evidence(self):
        for value in ["False", "0", "None"]:
            with self.subTest(value=value):
                result = self.retriever(f"def watch_file(value):\n    return Thread(value, daemon={value})\n").search("background watch file")
                self.assertEqual(result["hits"], [])
                self.assertIn("background", result["decision"]["missing_behaviors"])

    def test_daemon_attribute_requires_explicit_true_and_keeps_its_source_line(self):
        for value in ["False", "0", "None", "enabled"]:
            with self.subTest(value=value):
                result = self.retriever(f"def watch_file(worker):\n    worker.daemon = {value}\n    return worker\n").search("background watch file")
                self.assertEqual(result["hits"], [])
                self.assertIn("background", result["decision"]["missing_behaviors"])
        result = self.retriever("def watch_file(worker):\n    worker.daemon = True\n    return worker\n").search("background watch file")
        self.assertEqual(result["decision"]["status"], "accepted")
        evidence = [item for item in result["hits"][0]["behavior_evidence"] if item["behavior"] == "background"]
        self.assertEqual({item["line"] for item in evidence}, {2})

    def test_different_functions_and_nested_functions_cannot_assemble_behavior(self):
        for source in [
            "class Worker:\n    def expiration_cleanup(self, value):\n        return value\n    def watch_background(self, value):\n        return value\n",
            "def outer(value):\n    def expiration_cleanup():\n        return value\n    def watch_background():\n        return value\n    return value\n",
        ]:
            with self.subTest(source=source):
                result = self.retriever(source).search("expiration cleanup watch background")
                self.assertEqual(result["hits"], [])
                self.assertEqual(result["decision"]["reason"], "required_behaviors_not_in_same_scope")
        present = self.retriever("async def run(value):\n    await expiration_cleanup(value)\n    return background_watch(value)\n").search("expiration cleanup watch background")
        self.assertEqual(present["decision"]["status"], "accepted")
        self.assertEqual(len({item["scope"] for item in present["hits"][0]["behavior_evidence"]}), 1)

    def test_module_execution_is_an_independent_valid_scope(self):
        result = self.retriever("value = expiration_cleanup()\nbackground_watch(value)\n").search("expiration cleanup watch background")
        self.assertEqual(result["decision"]["status"], "accepted")
        self.assertEqual({item["scope"] for item in result["hits"][0]["behavior_evidence"]}, {"<module>"})

    def test_behavior_outside_the_returned_window_cannot_be_borrowed(self):
        source = "def checkpoint(value):\n    # expiration\n" + "    value = value + 1\n" * 100 + "    return expiration(value) # checkpoint\n"
        result = self.retriever(source).search("checkpoint expiration")
        self.assertTrue(result["candidates"])
        self.assertEqual(result["hits"], [])
        for candidate in result["candidates"]:
            self.assertTrue(candidate["missing_behaviors"])
            self.assertTrue(all(candidate["start_line"] <= item["line"] <= candidate["end_line"] for item in candidate["behavior_evidence"]))

    def test_continued_attribute_is_evidence_on_its_own_visible_line(self):
        source = "def run(worker):\n    # checkpoint\n" + "    value = 1\n" * 97 + "    return (worker\n        .checkpoint()\n    )\n"
        result = self.retriever(source).search("checkpoint")
        self.assertEqual(result["decision"]["status"], "accepted")
        self.assertTrue(all(hit["start_line"] > 100 for hit in result["hits"]))
        self.assertTrue(all(item["line"] == 101 for hit in result["hits"] for item in hit["behavior_evidence"]))

    def test_type_alias_declarations_cannot_supply_runtime_behavior(self):
        for source in [
            "type Metadata = checkpoint\n",
            "from typing import TypeAlias\nMetadata: TypeAlias = checkpoint\n",
        ]:
            with self.subTest(source=source):
                result = self.retriever(source).search("checkpoint")
                self.assertTrue(result["candidates"])
                self.assertEqual(result["hits"], [])

    def test_real_call_before_return_none_keeps_nonstub_function_name(self):
        source = "def checkpoint(value):\n    save(value)\n    return None\n"
        result = self.retriever(source).search("checkpoint")
        self.assertEqual(result["decision"]["status"], "accepted")

    def test_functions_with_only_excluded_or_nonrunning_bodies_are_not_evidence(self):
        for source in [
            "from typing import TYPE_CHECKING\ndef checkpoint():\n    if TYPE_CHECKING:\n        ordinary()\n",
            "def checkpoint():\n    if False:\n        ordinary()\n",
            "def checkpoint():\n    import ordinary\n",
            "def checkpoint():\n    from helpers import ordinary\n",
            "def checkpoint():\n    if True:\n        import ordinary\n",
            "def checkpoint():\n    if False:\n        ordinary()\n    else:\n        pass\n",
        ]:
            with self.subTest(source=source):
                result = self.retriever(source).search("checkpoint")
                self.assertTrue(result["candidates"])
                self.assertEqual(result["hits"], [])
                self.assertEqual(result["decision"]["missing_behaviors"], ["checkpoint"])
        present = self.retriever("def checkpoint():\n    if True:\n        ordinary()\n").search("checkpoint")
        self.assertEqual(present["decision"]["status"], "accepted")

    def test_boolean_if_tests_keep_operations_before_the_short_circuit(self):
        for source in [
            "def run():\n    if cleanup() or True:\n        return expiration()\n",
            "def cleanup():\n    if expiration() and False:\n        pass\n",
            "def cleanup():\n    if True and expiration() and False:\n        pass\n",
        ]:
            with self.subTest(source=source):
                result = self.retriever(source).search("cleanup expiration")
                self.assertEqual(result["decision"]["status"], "accepted")
                self.assertEqual(result["hits"][0]["missing_behaviors"], [])

    def test_boolean_if_tests_do_not_visit_short_circuited_operations(self):
        for test in ["False and expiration()", "True or expiration()",
                     "TYPE_CHECKING and expiration()", "not TYPE_CHECKING or expiration()"]:
            with self.subTest(test=test):
                source = f"from typing import TYPE_CHECKING\ndef cleanup():\n    if {test}:\n        ordinary()\n"
                result = self.retriever(source).search("cleanup expiration")
                self.assertEqual(result["hits"], [])
                self.assertIn("expiration", result["decision"]["missing_behaviors"])

    def test_comprehension_body_cannot_borrow_its_enclosing_function_name(self):
        for expression in [
            "(expiration(x) for x in items)",
            "[expiration(x) for x in items]",
            "{expiration(x) for x in items}",
            "{x: expiration(x) for x in items}",
            "(x for x in items if expiration(x))",
            "(x for group in items for x in expiration(group))",
            "(expiration(x) for x in cleanup(items))",
        ]:
            with self.subTest(expression=expression):
                result = self.retriever(f"def cleanup(items):\n    return {expression}\n").search("cleanup expiration")
                self.assertEqual(result["hits"], [])
                self.assertEqual(result["decision"]["reason"], "required_behaviors_not_in_same_scope")

    def test_comprehension_first_iterable_executes_in_the_enclosing_scope(self):
        result = self.retriever("def cleanup(items):\n    return (x for x in expiration(items))\n").search("cleanup expiration")
        self.assertEqual(result["decision"]["status"], "accepted")
        scopes = {item["scope"] for item in result["hits"][0]["behavior_evidence"]}
        self.assertEqual(len(scopes), 1)

    def test_one_comprehension_can_supply_both_required_behaviors(self):
        result = self.retriever("def run(items):\n    return (cleanup(expiration(x)) for x in items)\n").search("cleanup expiration")
        self.assertEqual(result["decision"]["status"], "accepted")
        self.assertTrue(all("comprehension" in item["scope"] for item in result["hits"][0]["behavior_evidence"]))

    def test_behavior_hits_fill_from_the_ranked_pool_after_rejecting_comments(self):
        for number in range(6):
            (self.root / f"fake{number}.py").write_text("# checkpoint\n", encoding="utf-8")
        retriever = self.retriever("def run(value):\n    return restore_checkpoint(value)\n", "real.py")
        small = retriever.search("checkpoint", limit=5)
        large = retriever.search("checkpoint", limit=20)
        self.assertEqual(len(small["candidates"]), 5)
        self.assertTrue(all(item["path"].startswith("fake") for item in small["candidates"]))
        self.assertEqual(small["decision"]["status"], "accepted")
        self.assertEqual([hit["path"] for hit in small["hits"]], ["real.py"])
        self.assertEqual(small["hits"], large["hits"])
        self.assertEqual(small["decision"]["missing_behaviors"], [])
        self.assertEqual(small["decision"]["missing_anchors"], [])

    def test_deep_valid_ast_failure_is_closed_and_diagnosed(self):
        source = "cache = checkpoint" + " + x" * 1200 + "\n"
        retriever = self.retriever(source)
        result = retriever.search("checkpoint")
        self.assertEqual(result["hits"], [])
        self.assertEqual(result["candidates"][0]["behavior_analysis"]["status"], "analysis_error")
        self.assertEqual(retriever.search("cache")["decision"]["status"], "accepted")

    def test_changed_source_invalidates_behavior_from_an_old_snapshot(self):
        retriever = self.retriever("def run(value):\n    return checkpoint(value)\n")
        self.assertEqual(retriever.search("checkpoint")["decision"]["status"], "accepted")
        (self.root / "workflow.py").write_text("def run(value):\n    return value\n", encoding="utf-8")
        self.assertEqual(retriever.search("checkpoint")["candidates"], [])

    def test_unsupported_or_invalid_python_fails_closed_only_for_behavior_gate(self):
        for name, source, status in [
            ("workflow.txt", "cache checkpoint\n", "unsupported_language"),
            ("workflow.py", "def cache_checkpoint(:\n", "syntax_error"),
        ]:
            with self.subTest(name=name):
                retriever = self.retriever(source, name)
                behavior = retriever.search("checkpoint")
                self.assertEqual(behavior["hits"], [])
                self.assertEqual(behavior["candidates"][0]["behavior_analysis"]["status"], status)
                self.assertEqual(retriever.search("cache")["decision"]["status"], "accepted")
                (self.root / name).unlink()

    def test_ast_node_budget_fails_closed_without_disabling_ordinary_search(self):
        source = "value = checkpoint()\n" + "value = value + 1\n" * 15000
        retriever = self.retriever(source)
        result = retriever.search("checkpoint")
        self.assertEqual(result["hits"], [])
        self.assertEqual(result["candidates"][0]["behavior_analysis"]["status"], "node_budget_exceeded")
        self.assertEqual(retriever.search("value")["decision"]["status"], "accepted")


if __name__ == "__main__":
    unittest.main()
