"""Real indexing and runnable paired evidence for the v4 vocabulary changes."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from paicli.index import CodeIndex
from paicli.retrieval import CodeRetriever
from paicli.storage import Store
from paicli.workspace import Workspace


class PrecisionRecallTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.workspace = Workspace(self.root)
        self.store = Store(self.workspace)
        self.addCleanup(self.store.close)
        self.index = CodeIndex(self.workspace, self.store)

    def retrieve(self, source, query, name="sample.py"):
        (self.root / name).write_text(source, encoding="utf-8")
        self.index.build()
        return CodeRetriever(self.index).search(query)

    def test_version_and_excerpt_questions_accept_relevant_code(self):
        cases = [
            ('__version__ = "2.4.1"\n',
             ["What version string does the package export?", "项目版本号"]),
            ("def collect_excerpts(dropped):\n"
             "    excerpts = [text[:12] for text in dropped[-2:]]\n"
             "    return excerpts\n",
             ["How many excerpts are kept from earlier turns and how long are they?", "摘录", "节选"]),
        ]
        for source, queries in cases:
            for query in queries:
                with self.subTest(query=query):
                    result = self.retrieve(source, query)
                    self.assertEqual(result["decision"]["status"], "accepted")
                    self.assertTrue(result["hits"])
                    self.assertEqual(result["hits"][0]["path"], "sample.py")

    def test_new_ordinary_concepts_do_not_accept_unrelated_code(self):
        for query in ("What version string does the package export?",
                      "How many excerpts are kept from earlier turns and how long are they?",
                      "版本号", "摘录"):
            with self.subTest(query=query):
                result = self.retrieve("def add(left, right):\n    return left + right\n", query)
                self.assertEqual(result["decision"]["status"], "insufficient_evidence")
                self.assertEqual(result["hits"], [])

    def test_concept_aliases_count_once_and_keep_explicit_identifiers_required(self):
        source = '__version__ = "2.4.1"\n'
        result = self.retrieve(source, "version versions")
        self.assertEqual(result["decision"]["status"], "accepted")
        self.assertEqual(result["query"]["gate_concepts"], ["version"])
        result = self.retrieve(source, "What version string does `MissingRelease` export?")
        self.assertEqual(result["hits"], [])
        self.assertIn("missingrelease", result["decision"]["missing_anchors"])

    def test_jitter_requires_code_and_accepts_a_runnable_calculation(self):
        positive = (
            "def request_retry_delay(attempt, jitter):\n"
            "    return 2 ** attempt + jitter\n")
        namespace = {}
        exec(compile(positive, "jitter_fixture.py", "exec"), namespace)
        self.assertEqual(namespace["request_retry_delay"](2, 0.25), 4.25)
        negative = (
            "def request_retry_delay(attempt, offset):\n"
            "    # jitter and random backoff are future work\n"
            "    return 2 ** attempt\n")
        for query in ("How does a request retry calculate its jitter?", "请求重试的抖动如何计算？"):
            with self.subTest(query=query):
                self.assertEqual(self.retrieve(negative, query)["hits"], [])
                present = self.retrieve(positive, query)
                self.assertEqual(present["decision"]["status"], "accepted")
                self.assertIn("jitter", present["hits"][0]["matched_behaviors"])

    def test_synonyms_require_code_and_accept_a_runnable_mapping(self):
        positive = (
            "def search_synonyms(term, synonyms):\n"
            "    return synonyms.get(term, [term])\n")
        namespace = {}
        exec(compile(positive, "synonym_fixture.py", "exec"), namespace)
        self.assertEqual(namespace["search_synonyms"]("car", {"car": ["auto"]}), ["auto"])
        negative = (
            "def search_terms(term):\n"
            "    # synonyms and thesaurus support are future work\n"
            "    return [term]\n")
        for query in ("How does search use synonyms?", "搜索如何使用同义词？"):
            with self.subTest(query=query):
                self.assertEqual(self.retrieve(negative, query)["hits"], [])
                self.assertEqual(self.retrieve(positive, query)["decision"]["status"], "accepted")

    def test_tokenizer_and_pricing_require_both_and_accept_real_token_cost(self):
        positive = (
            "def model_request_context(text, model, prices):\n"
            "    tokenizer = str.split\n"
            "    tokens = tokenizer(text)\n"
            "    token_price = prices[model]\n"
            "    return len(tokens) * token_price\n")
        namespace = {}
        exec(compile(positive, "pricing_fixture.py", "exec"), namespace)
        self.assertEqual(namespace["model_request_context"]("two words", "demo", {"demo": 0.01}), 0.02)
        negative = (
            "def model_request_context(text):\n"
            "    # tokenizer and pricing are not implemented\n"
            "    return text[-5:]\n")
        queries = (
            "How does model request context handling combine tokenization with pricing?",
            "模型请求的上下文如何分词并计价？",
        )
        for query in queries:
            with self.subTest(query=query):
                self.assertEqual(self.retrieve(negative, query)["hits"], [])
                result = self.retrieve(positive, query)
                self.assertEqual(result["decision"]["status"], "accepted")
                self.assertEqual(set(result["hits"][0]["matched_behaviors"]), {"tokenization", "pricing"})

    def test_capability_names_in_imports_strings_or_placeholders_do_not_prove_implementation(self):
        fakes = (
            "from future import tokenizer, pricing\n",
            'def run():\n    return "tokenizer pricing"\n',
            "def tokenizer_pricing():\n    pass\n",
            "def tokenizer_pricing():\n    raise NotImplementedError()\n",
        )
        for source in fakes:
            with self.subTest(source=source):
                self.assertEqual(self.retrieve(source, "tokenization pricing")["hits"], [])

    def test_capabilities_must_be_supported_in_the_same_execution_scope(self):
        source = (
            "class Service:\n"
            "    def tokenize(self, text):\n"
            "        return text.split()\n"
            "    def pricing(self, count):\n"
            "        return count * 0.01\n")
        self.assertEqual(self.retrieve(source, "tokenization pricing")["hits"], [])

    def test_generic_retry_token_and_cost_questions_keep_their_existing_policy(self):
        source = (
            "def validate_tool_call(call):\n"
            "    if call is None:\n"
            '        raise ValueError("invalid call; do not retry")\n'
            "    return call\n")
        query = "Does validation retry an invalid tool call?"
        result = self.retrieve(source, query)
        self.assertEqual(result["decision"]["status"], "accepted")
        self.assertEqual(result["query"]["required_behaviors"], [])
        result = self.retrieve("def token_cost(token):\n    return len(token)\n", "token cost")
        self.assertEqual(result["decision"]["status"], "accepted")
        self.assertEqual(result["query"]["required_behaviors"], [])

    def test_cli_returns_version_evidence_and_hides_unsupported_capability_source(self):
        self.retrieve('__version__ = "2.4.1"\n', "version")
        command = [sys.executable, "-m", "paicli", "retrieve", "What version string does the package export?",
                   "--workspace", str(self.root)]
        run = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", timeout=20)
        self.assertEqual(run.returncode, 0, run.stderr)
        payload = json.loads(run.stdout)
        self.assertEqual(payload["decision"]["status"], "accepted")
        self.assertNotIn("candidates", payload)
        self.retrieve("def model_request_context(text):\n    return text[-5:]\n", "context")
        command[4] = "How does model request context handling combine tokenization with pricing?"
        run = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", timeout=20)
        self.assertEqual(run.returncode, 0, run.stderr)
        payload = json.loads(run.stdout)
        self.assertEqual(payload["decision"]["status"], "insufficient_evidence")
        self.assertEqual(payload["hits"], [])
        self.assertNotIn("return text", run.stdout)


if __name__ == "__main__":
    unittest.main()
