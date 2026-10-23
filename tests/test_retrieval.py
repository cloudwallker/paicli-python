from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from paicli.errors import PolicyError
from paicli.index import CodeIndex
from paicli.storage import Store
from paicli.workspace import Workspace


class RetrievalTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.workspace = Workspace(self.root)
        self.store = Store(self.workspace)
        self.addCleanup(self.store.close)
        self.index = CodeIndex(self.workspace, self.store)

    def make_retriever(self, files):
        from paicli.retrieval import CodeRetriever

        for name, content in files.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content.encode("utf-8"))
        self.index.build()
        return CodeRetriever(self.index)

    def test_chinese_query_matches_english_code_without_chinese_comments(self):
        retriever = self.make_retriever({
            "memory.py": "def compact_context(messages):\n    return messages[-3:]\n",
            "math.py": "def add_numbers(left, right):\n    return left + right\n",
        })
        result = retriever.search("如何压缩上下文？")
        self.assertEqual(result["decision"]["status"], "accepted")
        self.assertEqual(result["hits"][0]["path"], "memory.py")
        self.assertEqual(result["hits"][0]["lexical_coverage"], 1.0)
        self.assertEqual(len(result["query"]["concepts"]), 2)

    def test_snake_case_camel_case_and_acronyms_are_searchable(self):
        retriever = self.make_retriever({
            "transport.py": "def sendHTTPRequest(timeout_seconds):\n    return timeout_seconds\n",
            "other.py": "def send_command(value):\n    return value\n",
        })
        for query in ["http request timeout", "HTTPRequest 超时", "timeout_seconds"]:
            with self.subTest(query=query):
                self.assertEqual(retriever.search(query)["hits"][0]["path"], "transport.py")

    def test_synonyms_do_not_inflate_concept_coverage(self):
        retriever = self.make_retriever({"memory.py": "def compact_context(messages):\n    return messages\n"})
        result = retriever.search("上下文 context 压缩 compact")
        self.assertEqual(len(result["query"]["concepts"]), 2)
        self.assertEqual(result["hits"][0]["lexical_coverage"], 1.0)

    def test_natural_language_details_are_soft_evidence_not_required_entities(self):
        retriever = self.make_retriever({"memory.py": "def compact_context(history):\n    return history[-2:]\n"})
        for query in [
            "How does context compaction keep earlier conversation turns around when the user continues talking?",
            "以前聊了许多内容，现在又说了好多话，历史上下文如何压缩才更方便继续交流？",
        ]:
            with self.subTest(query=query):
                result = retriever.search(query)
                self.assertEqual(result["decision"]["status"], "accepted")
                self.assertTrue(result["query"]["soft_terms"])
                self.assertEqual(result["query"]["mode"], "natural_language")
                self.assertEqual(result["hits"][0]["evidence_coverage"], 1.0)
                self.assertLess(result["hits"][0]["lexical_coverage"], 1.0)

    def test_explicit_entities_stay_required_inside_long_questions(self):
        retriever = self.make_retriever({"memory.py": "def compact_context(history):\n    return history[-2:]\n"})
        for query in [
            "How does Nebulark compact context while keeping earlier conversation turns around?",
            "How does ZTR compact context while keeping earlier conversation turns around?",
            "How does `nebulark` compact context while keeping earlier conversation turns around?",
            "历史上下文如何压缩，Nebulark 又怎样照顾之前说过的内容并继续聊天？",
        ]:
            with self.subTest(query=query):
                result = retriever.search(query)
                self.assertTrue(result["candidates"])
                self.assertEqual(result["hits"], [])
                self.assertTrue(result["decision"].get("missing_anchors"))
                self.assertTrue(result["query"].get("required_anchors"))

    def test_uppercase_anchor_takes_precedence_over_stopwords(self):
        retriever = self.make_retriever({"memory.py": "def cache_file():\n    return 1\n"})
        result = retriever.search("GET file")
        self.assertEqual(result["hits"], [])
        self.assertEqual(result["decision"]["missing_anchors"], ["get"])
        self.assertEqual(result["query"]["required_anchors"][0]["term"], "get")

    def test_explicit_anchor_can_retrieve_without_nonstopword_concepts(self):
        retriever = self.make_retriever({"memory.py": "def get():\n    return 1\n"})
        result = retriever.search("`get`")
        self.assertEqual(result["decision"]["status"], "accepted")
        self.assertEqual(result["decision"]["missing_anchors"], [])
        self.assertEqual(result["hits"][0]["path"], "memory.py")
        self.assertGreater(result["hits"][0]["score"], 0)
        self.assertEqual(result["hits"], retriever.search("`get`")["hits"])
        absent = retriever.search("`make`")
        self.assertEqual(absent["hits"], [])
        self.assertEqual(absent["decision"]["reason"], "unsupported_required_anchors")
        self.assertEqual(absent["decision"]["missing_anchors"], ["make"])

    def test_identifier_anchor_cannot_be_assembled_from_separate_identifiers(self):
        retriever = self.make_retriever({
            "memory.py": "def compact_context(history):\n    nebulark = history\n    adapter = history\n    return history[-2:]\n",
        })
        result = retriever.search("nebulark_adapter compact context")
        self.assertTrue(result["candidates"])
        self.assertEqual(result["hits"], [])
        self.assertIn("nebulark_adapter", result["decision"]["missing_anchors"])
        (self.root / "memory.py").write_text("def nebularkAdapter_compact_context(history):\n    return history[-2:]\n", encoding="utf-8")
        self.index.build()
        from paicli.retrieval import CodeRetriever

        present = CodeRetriever(self.index).search("How does nebulark_adapter compact context while keeping earlier conversation turns around?")
        self.assertEqual(present["decision"]["status"], "accepted")

    def test_soft_term_policy_does_not_create_evidence_without_known_concepts(self):
        retriever = self.make_retriever({"memory.py": "def cache_values(value):\n    return value\n"})
        result = retriever.search("earlier visitors patiently linger nearby")
        self.assertEqual(result["hits"], [])
        self.assertEqual(result["decision"]["status"], "insufficient_evidence")

    def test_unknown_technology_is_matched_from_each_workspace_not_a_blacklist(self):
        retriever = self.make_retriever({"store.py": "def cache_zorbix(value):\n    return value\n"})
        present = retriever.search("Zorbix 缓存")
        self.assertEqual(present["decision"]["status"], "accepted")
        self.assertEqual(present["hits"][0]["path"], "store.py")
        (self.root / "store.py").write_text("def cache_file_path(value):\n    return value\n", encoding="utf-8")
        self.index.build()
        from paicli.retrieval import CodeRetriever

        absent = CodeRetriever(self.index).search("Zorbix 缓存 文件 路径")
        self.assertEqual(absent["hits"], [])
        self.assertTrue(absent["candidates"])
        self.assertEqual(absent["decision"]["best_coverage"], 0.75)
        self.assertIn("zorbix", absent["decision"]["unsupported_terms"])
        self.assertEqual(absent["decision"]["status"], "insufficient_evidence")

    def test_generic_question_has_no_evidence_and_unknown_chinese_is_not_discarded(self):
        retriever = self.make_retriever({"store.py": "def cache_values(value):\n    return value\n"})
        for query in ["这个项目如何实现功能", "量子仙术", "缓存量子仙术"]:
            with self.subTest(query=query):
                result = retriever.search(query)
                self.assertEqual(result["hits"], [])
                self.assertEqual(result["decision"]["status"], "insufficient_evidence")
        self.assertEqual(retriever.search("这个项目如何实现功能")["candidates"], [])
        self.assertIn("量子仙术", retriever.search("缓存量子仙术")["decision"]["unsupported_terms"])

    def test_unicode_letters_and_extended_han_are_never_silently_discarded(self):
        retriever = self.make_retriever({"store.py": "def cache_caf():\n    return 1\n"})
        for query, unknown in [("café 缓存", "café"), ("cafe\u0301 缓存", "café"), ("缓存\U00020bb7", "\U00020bb7")]:
            with self.subTest(query=query):
                result = retriever.search(query)
                self.assertTrue(result["candidates"])
                self.assertEqual(result["hits"], [])
                self.assertIn(unknown, result["decision"]["unsupported_terms"])

    def test_unicode_normalization_matches_equivalent_source_and_query_spellings(self):
        retriever = self.make_retriever({"unicode.py": "def cache_cafe\u0301():\n    return '\U00020bb7'\n"})
        nfc = retriever.search("café 缓存")
        nfd = retriever.search("cafe\u0301 缓存")
        self.assertEqual(nfc["decision"]["status"], "accepted")
        self.assertEqual(nfc["hits"], nfd["hits"])
        self.assertEqual(nfc["query"]["concepts"], nfd["query"]["concepts"])
        self.assertEqual(retriever.search("缓存\U00020bb7")["decision"]["status"], "accepted")
        self.assertIn("cafe\u0301", nfc["hits"][0]["content"])

    def test_unknown_noncomposing_letter_marks_are_not_reduced_to_ascii(self):
        retriever = self.make_retriever({"store.py": "def cache_q():\n    return 1\n"})
        result = retriever.search("q\u0301 缓存")
        self.assertEqual(result["hits"], [])
        self.assertIn("q\u0301", result["decision"]["unsupported_terms"])

    def test_only_visible_content_can_support_a_match(self):
        retriever = self.make_retriever({"cache.py": "answer = 42\n"})
        self.assertEqual(retriever.search("缓存")["candidates"], [])

    def test_gate_keeps_raw_candidates_when_only_half_the_concepts_match(self):
        retriever = self.make_retriever({
            "one.py": "def cache_values(value):\n    return value\n",
            "two.py": "def acquire_permission():\n    return True\n",
        })
        result = retriever.search("缓存 权限")
        self.assertTrue(result["candidates"])
        self.assertEqual(result["decision"]["best_coverage"], 0.5)
        self.assertEqual(result["decision"]["threshold"], 0.6)
        self.assertEqual(result["hits"], [])

    def test_parameter_boundaries_reject_bool_empty_and_oversized_values(self):
        retriever = self.make_retriever({"a.py": "x = 1\n"})
        for query in [None, 1, True, "", "  ", "x" * 501]:
            with self.subTest(query=query):
                with self.assertRaises(PolicyError):
                    retriever.search(query)
        for limit in [True, False, 0, 21, 1.5, "5"]:
            with self.subTest(limit=limit):
                with self.assertRaises(PolicyError):
                    retriever.search("x", limit=limit)
        self.assertEqual(retriever.search("x", limit=1)["decision"]["status"], "accepted")
        self.assertEqual(retriever.search("x" * 500, limit=20)["hits"], [])

    def test_snapshot_and_results_are_reusable_without_query_state_leaks_or_writes(self):
        retriever = self.make_retriever({"a.py": "def cache_context(value):\n    return value\n"})
        before = self.store.connection.total_changes
        first = retriever.search("缓存")
        expected_profile = dict(first["profile"])
        first["hits"][0]["content"] = "tampered"
        first["query"]["concepts"].clear()
        retriever.search("missingtechnology")
        again = retriever.search("缓存")
        self.assertNotEqual(again["hits"][0]["content"], "tampered")
        self.assertTrue(again["query"]["concepts"])
        self.assertEqual(again["profile"], expected_profile)
        self.assertEqual(self.store.connection.total_changes, before)
        from paicli.retrieval import CodeRetriever

        self.assertEqual(CodeRetriever(self.index).search("上下文")["profile"]["profile_hash"], expected_profile["profile_hash"])

    def test_returned_policy_explanation_cannot_mutate_the_frozen_profile(self):
        retriever = self.make_retriever({"a.py": "def cache_context(value):\n    return value\n"})
        first = retriever.search("缓存")
        original = first["profile"]["anchor_rules"]["matching"]
        first["profile"]["anchor_rules"]["matching"] = "tampered"
        self.assertEqual(retriever.search("缓存")["profile"]["anchor_rules"]["matching"], original)

    def test_changed_or_deleted_sources_are_excluded_before_and_after_snapshot(self):
        retriever = self.make_retriever({"a.py": "def cache_values():\n    return 1\n"})
        (self.root / "a.py").write_text("def replaced():\n    return 2\n", encoding="utf-8")
        self.assertEqual(retriever.search("缓存")["candidates"], [])
        from paicli.retrieval import CodeRetriever

        self.assertEqual(CodeRetriever(self.index).search("缓存")["candidates"], [])
        (self.root / "a.py").unlink()
        self.assertEqual(retriever.search("缓存")["candidates"], [])

    def test_snapshot_fingerprint_distinguishes_visible_chunks_of_the_same_source(self):
        retriever = self.make_retriever({"a.py": "def cache_values():\n    return 1\n\ndef request_values():\n    return 2\n"})
        original = retriever.search("缓存")["profile"]
        with self.store.connection:
            self.store.connection.execute("DELETE FROM chunks WHERE symbol = ?", ("cache_values",))
        from paicli.retrieval import CodeRetriever

        changed = CodeRetriever(self.index).search("缓存")
        self.assertEqual(changed["candidates"], [])
        self.assertNotEqual(changed["profile"]["snapshot_hash"], original["snapshot_hash"])
        self.assertEqual(changed["profile"]["profile_hash"], original["profile_hash"])

    def test_injected_protected_path_and_forged_content_cannot_be_returned(self):
        self.make_retriever({"a.py": "answer = 42\n"})
        con = self.store.connection
        with con:
            con.execute("UPDATE chunks SET content = 'cache_secret = 123\n'")
            con.execute("INSERT INTO chunks(path,symbol,start_line,end_line,content,digest) VALUES(?,?,?,?,?,?)", (".env", "cache", 1, 1, "cache_secret = 123\n", "irrelevant"))
        from paicli.retrieval import CodeRetriever

        self.assertEqual(CodeRetriever(self.index).search("cache")["candidates"], [])

    def test_long_chunk_returns_exact_complete_lines_and_can_match_the_tail(self):
        source = "".join(f"row_{n} = '{'a' * 110}'\n" for n in range(90)) + "needle_token = 'present'\n"
        retriever = self.make_retriever({"long.py": source})
        hit = retriever.search("needle_token")["hits"][0]
        self.assertLessEqual(len(hit["content"]), 8000)
        self.assertEqual(hit["content"], "".join(source.splitlines(keepends=True)[hit["start_line"] - 1:hit["end_line"]]))
        self.assertIn("needle_token", hit["content"])
        self.assertEqual(hit["end_line"], 91)

    def test_overlong_single_line_is_never_sliced_into_fabricated_evidence(self):
        retriever = self.make_retriever({"wide.txt": "hiddenneedle " + "x" * 8000 + "\nvisiblemarker\n"})
        self.assertEqual(retriever.search("hiddenneedle")["candidates"], [])
        result = retriever.search("visiblemarker")
        self.assertEqual(result["hits"][0]["content"], "visiblemarker\n")
        self.assertEqual(result["hits"][0]["start_line"], 2)
        self.assertGreater(result["profile"]["omitted_oversize_lines"], 0)

    def test_crlf_and_unicode_separator_keep_source_line_numbers(self):
        source = 'LABEL = "before\u2028after"\r\n\r\ndef cache_context():\r\n    return LABEL\r\n'
        retriever = self.make_retriever({"mixed.py": source})
        hit = retriever.search("缓存 上下文")["hits"][0]
        self.assertEqual((hit["start_line"], hit["end_line"]), (3, 4))
        self.assertEqual(hit["content"], "def cache_context():\r\n    return LABEL\r\n")

    def test_chunk_count_budget_rejects_the_whole_snapshot(self):
        self.make_retriever({"a.py": "x = 1\n"})
        con = self.store.connection
        row = con.execute("SELECT path,symbol,start_line,end_line,content,digest FROM chunks LIMIT 1").fetchone()
        with con:
            con.executemany("INSERT INTO chunks(path,symbol,start_line,end_line,content,digest) VALUES(?,?,?,?,?,?)", [row] * 10000)
        from paicli.retrieval import CodeRetriever

        with self.assertRaises(PolicyError):
            CodeRetriever(self.index)

    def test_aggregate_content_budget_rejects_the_whole_snapshot(self):
        self.make_retriever({"a.py": "x = 1\n"})
        con = self.store.connection
        with con:
            con.execute("UPDATE chunks SET content = ?", ("中" * (3 * 1024 * 1024),))
        from paicli.retrieval import CodeRetriever

        with self.assertRaises(PolicyError):
            CodeRetriever(self.index)

    def test_corrupt_binary_chunk_content_is_a_controlled_policy_error(self):
        self.make_retriever({"a.py": "x = 1\n"})
        with self.store.connection:
            self.store.connection.execute("UPDATE chunks SET content = ?", (b"not-text",))
        from paicli.retrieval import CodeRetriever

        with self.assertRaises(PolicyError):
            CodeRetriever(self.index)

    def test_per_source_budget_is_enforced_even_after_snapshot_construction(self):
        retriever = self.make_retriever({"a.py": "x = 1\n"})
        oversized = b"x" * (512 * 1024 + 1)
        (self.root / "a.py").write_bytes(oversized)
        with self.assertRaises(PolicyError):
            retriever.search("x")
        from paicli.retrieval import CodeRetriever

        with self.assertRaises(PolicyError):
            CodeRetriever(self.index)


if __name__ == "__main__":
    unittest.main()
