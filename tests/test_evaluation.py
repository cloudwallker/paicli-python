"""Evaluation contracts use hand-checked source fixtures and local model doubles."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from contextlib import asynccontextmanager
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from paicli.errors import PolicyError
from paicli.models import OpenAIModel, ScriptedModel
from paicli.workspace import Workspace


def api():
    from paicli.evaluation import evaluate_answers, evaluate_retrieval
    return evaluate_retrieval, evaluate_answers


class RetrievalEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.sources = {
            "a.py": "def beacon():\n    return 1\n",
            "b.py": "def beacon():\n    return 2\n",
        }
        for path, content in self.sources.items():
            (self.root / path).write_text(content, encoding="utf-8", newline="")
        self.workspace = Workspace(self.root)
        self.dataset = {
            "name": "hand_checked", "version": 1,
            "annotation_note": "Constructed fixture, hand checked line ranges.",
            "source_files": {path: hashlib.sha256(text.encode()).hexdigest()
                             for path, text in self.sources.items()},
            "cases": [
                self.case("both", "beacon", True, [self.span("a.py"), self.span("b.py")]),
                self.case("miss", "nonexistentquartz", True, [self.span("a.py")]),
                self.case("empty", "nonexistentquartz", False, []),
                self.case("false", "beacon", False, []),
            ],
        }

    @staticmethod
    def span(path, start=1, end=2):
        return {"path": path, "start_line": start, "end_line": end}

    @staticmethod
    def case(case_id, query, answerable, spans):
        return {"id": case_id, "query": query, "answerable": answerable,
                "relevant_spans": spans, "category": "fixture"}

    def test_recall_mrr_and_unanswerable_denominators_are_hand_calculated(self):
        evaluate_retrieval, _ = api()
        report = evaluate_retrieval(self.dataset, self.workspace, k=1)
        rows = report["methods"]["bm25"]["cases"]
        metrics = report["methods"]["bm25"]["metrics"]
        self.assertEqual((rows[0]["recall_at_k"], rows[0]["reciprocal_rank"]), (0.5, 1.0))
        self.assertEqual((rows[1]["recall_at_k"], rows[1]["reciprocal_rank"]), (0.0, 0.0))
        self.assertIsNone(rows[2]["recall_at_k"])
        self.assertEqual(metrics["answerable_cases"], 2)
        self.assertEqual(metrics["unanswerable_cases"], 2)
        self.assertEqual(metrics["recall_at_k"], 0.25)
        self.assertEqual(metrics["mrr_at_k"], 0.5)
        self.assertEqual(metrics["empty_result_rate"], 0.5)
        self.assertEqual(metrics["false_retrieval_rate"], 0.5)
        self.assertEqual(metrics["false_retrieval_count"], 1)
        self.assertEqual(report["source_files"], self.dataset["source_files"])
        self.assertEqual(rows[0]["hits"][0]["content"], self.sources["a.py"])
        latencies = sorted(row["latency_ms"] for row in rows)
        self.assertGreaterEqual(latencies[0], 0)
        self.assertAlmostEqual(metrics["latency_ms"]["p50"], (latencies[1] + latencies[2]) / 2)
        self.assertAlmostEqual(metrics["latency_ms"]["p95"], latencies[2] * .15 + latencies[3] * .85)
        self.assertGreaterEqual(report["timing_ms"]["graph_build"], 0)

    def test_selective_metrics_keep_abstentions_in_full_answerable_denominator(self):
        from paicli.evaluation import _retrieval_metrics, _score_case
        hit = {"path": "a.py", "start_line": 1, "end_line": 2,
               "symbol": "beacon", "content": self.sources["a.py"], "score": 1.0}
        rows = [
            _score_case(self.dataset["cases"][0], [hit], .1),  # accepted, recall 1/2
            _score_case(self.case("wrong", "beacon", True, [self.span("b.py")]), [hit], .1),
            _score_case(self.dataset["cases"][1], [], .1),  # abstained, recall zero
            _score_case(self.dataset["cases"][2], [], .1),
            _score_case(self.dataset["cases"][3], [hit], .1),
        ]
        metrics = _retrieval_metrics(rows)
        self.assertEqual(metrics.get("answerable_accepted_count"), 2)
        self.assertEqual(metrics["answerable_abstained_count"], 1)
        self.assertEqual(metrics["unanswerable_accepted_count"], 1)
        self.assertEqual(metrics["unanswerable_abstained_count"], 1)
        self.assertAlmostEqual(metrics["recall_at_k"], 1 / 6)
        self.assertEqual(metrics["accepted_answerable_recall"], .25)
        self.assertAlmostEqual(metrics["answerable_false_abstention_rate"], 1 / 3)
        self.assertEqual(metrics["abstention_count"], 2)
        self.assertEqual(metrics["abstention_rate"], .4)
        self.assertEqual(metrics["false_retrieval_rate"], .5)

    def test_selective_metrics_report_unknown_rates_when_denominators_are_empty(self):
        from paicli.evaluation import _retrieval_metrics, _score_case
        answerable = _retrieval_metrics([_score_case(self.dataset["cases"][1], [], .1)])
        self.assertIn("accepted_answerable_recall", answerable)
        self.assertIsNone(answerable["accepted_answerable_recall"])
        self.assertEqual(answerable["recall_at_k"], 0)
        self.assertEqual(answerable["answerable_false_abstention_rate"], 1)
        self.assertIsNone(answerable["false_retrieval_rate"])
        unanswerable = _retrieval_metrics([_score_case(self.dataset["cases"][2], [], .1)])
        self.assertIsNone(unanswerable["recall_at_k"])
        self.assertIsNone(unanswerable["accepted_answerable_recall"])
        self.assertIsNone(unanswerable["answerable_false_abstention_rate"])
        empty = _retrieval_metrics([])
        self.assertIsNone(empty["abstention_rate"])

    def test_report_exposes_raw_and_gated_retrieval_with_category_denominators(self):
        evaluate_retrieval, _ = api()
        dataset = copy.deepcopy(self.dataset)
        dataset["cases"][0]["category"] = "symbol"
        dataset["cases"][1]["category"] = "unmatched"
        dataset["cases"][2]["category"] = "unmatched"
        dataset["cases"][3]["category"] = "symbol"
        report = evaluate_retrieval(dataset, self.workspace, k=1)
        self.assertEqual(set(report["methods"]), {"bm25", "graph_expand", "code_aware", "code_aware_gated"})
        self.assertGreaterEqual(report["timing_ms"]["code_retriever_build"], 0)
        self.assertIsInstance(report["retrieval_profile"], dict)
        for method in report["methods"].values():
            groups = method["by_category"]
            self.assertEqual(set(groups), {"symbol", "unmatched"})
            self.assertEqual(groups["symbol"]["total_cases"], 2)
            self.assertEqual(groups["symbol"]["answerable_cases"], 1)
            self.assertEqual(groups["symbol"]["unanswerable_cases"], 1)
            self.assertEqual(groups["symbol"]["recall_at_k"], .5)
            self.assertEqual(groups["symbol"]["mrr_at_k"], 1)
            self.assertEqual(groups["symbol"]["false_retrieval_rate"], 1)
            self.assertEqual(groups["symbol"]["abstention_rate"], 0)
            self.assertEqual(groups["unmatched"]["recall_at_k"], 0)
            self.assertEqual(groups["unmatched"]["abstention_rate"], 1)
        raw = report["methods"]["code_aware"]["cases"]
        gated = report["methods"]["code_aware_gated"]["cases"]
        for raw_row, gated_row in zip(raw, gated):
            self.assertEqual(raw_row["retrieval"], gated_row["retrieval"])
            self.assertEqual(set(raw_row["retrieval"]), {"decision", "query", "profile"})
            self.assertEqual(raw_row["retrieval"]["decision"]["threshold"], .6)
            self.assertEqual(raw_row["retrieval"]["profile"], report["retrieval_profile"])
            self.assertEqual(gated_row["accepted"], bool(gated_row["hits"]))
            self.assertEqual(gated_row["abstained"], not gated_row["hits"])

    def test_raw_candidates_are_scored_separately_from_gate_abstention(self):
        evaluate_retrieval, _ = api()
        dataset = copy.deepcopy(self.dataset)
        dataset["cases"] = [self.case("mixed", "beacon quartzmissing zirconmissing", True,
                                      [self.span("a.py"), self.span("b.py")])]
        report = evaluate_retrieval(dataset, self.workspace, k=1)
        raw = report["methods"]["code_aware"]
        gated = report["methods"]["code_aware_gated"]
        self.assertEqual(len(raw["cases"][0]["hits"]), 1)
        self.assertTrue(raw["cases"][0]["accepted"])
        self.assertEqual(raw["cases"][0]["retrieval"]["decision"]["status"], "insufficient_evidence")
        self.assertEqual(gated["cases"][0]["hits"], [])
        self.assertEqual(raw["metrics"]["recall_at_k"], .5)
        self.assertEqual(gated["metrics"]["answerable_cases"], 1)
        self.assertEqual(gated["metrics"]["recall_at_k"], 0)
        self.assertIsNone(gated["metrics"]["accepted_answerable_recall"])
        self.assertEqual(gated["metrics"]["answerable_false_abstention_rate"], 1)

    def test_retriever_is_built_once_from_source_only_snapshot_and_ignores_labels(self):
        from paicli.retrieval import CodeRetriever
        evaluate_retrieval, _ = api()
        (self.root / "README.md").write_text("annotation_only_marker", encoding="utf-8")
        (self.root / "cases.json").write_text(json.dumps(self.dataset), encoding="utf-8")
        (self.root / "unlisted.py").write_text("annotation_only_marker = 1", encoding="utf-8")
        snapshot_files = []

        def construct(index):
            snapshot_files.append(index.workspace.files()[0])
            return CodeRetriever(index)

        with patch("paicli.retrieval.CodeRetriever", side_effect=construct) as build:
            original = evaluate_retrieval(self.dataset, self.workspace, k=1)
        self.assertEqual(build.call_count, 1)
        self.assertEqual(snapshot_files, [["a.py", "b.py"]])
        changed = copy.deepcopy(self.dataset)
        changed["annotation_note"] = "annotation_only_marker"
        for case in changed["cases"]:
            case["category"] = "changed_label_marker"
            case["answerable"] = not case["answerable"]
            case["relevant_spans"] = [self.span("b.py")] if case["answerable"] else []
        updated = evaluate_retrieval(changed, self.workspace, k=1)
        self.assertEqual(original["retrieval_profile"], updated["retrieval_profile"])
        for method in ("code_aware", "code_aware_gated"):
            for before, after in zip(original["methods"][method]["cases"],
                                     updated["methods"][method]["cases"]):
                self.assertEqual(before["hits"], after["hits"])
                self.assertEqual(before["retrieval"], after["retrieval"])
                self.assertEqual(before["accepted"], after["accepted"])
                metadata = json.dumps(after["retrieval"], ensure_ascii=False)
                for forbidden in ("annotation_only_marker", "changed_label_marker", "relevant_spans",
                                  "answerable", "README.md", "cases.json", "unlisted.py"):
                    self.assertNotIn(forbidden, metadata)
        self.assertFalse((self.root / ".paicli").exists())

    def test_only_manifest_sources_are_indexed_and_no_original_store_is_created(self):
        evaluate_retrieval, _ = api()
        (self.root / "README.md").write_text("nonexistentquartz", encoding="utf-8")
        (self.root / "cases.json").write_text(json.dumps(self.dataset), encoding="utf-8")
        (self.root / "unlisted.py").write_text("nonexistentquartz = 1", encoding="utf-8")
        report = evaluate_retrieval(self.dataset, self.workspace)
        for method in report["methods"].values():
            self.assertEqual(method["cases"][1]["hits"], [])
        self.assertFalse((self.root / ".paicli").exists())

    def test_source_digest_change_rejects_stale_annotations(self):
        evaluate_retrieval, _ = api()
        (self.root / "a.py").write_text("def changed():\n    return 3\n", encoding="utf-8")
        with self.assertRaises(PolicyError):
            evaluate_retrieval(self.dataset, self.workspace)

    def test_cumulative_source_budget_rejects_many_individually_valid_files(self):
        evaluate_retrieval, _ = api()
        content = b"#" + b"x" * (512 * 1024 - 2) + b"\n"
        dataset = copy.deepcopy(self.dataset)
        dataset["source_files"] = {}
        for number in range(17):
            path = f"large{number}.py"
            (self.root / path).write_bytes(content)
            dataset["source_files"][path] = hashlib.sha256(content).hexdigest()
        dataset["cases"] = [self.case("large", "x", True, [self.span("large0.py", 1, 1)])]
        with self.assertRaisesRegex(PolicyError, "corpus"):
            evaluate_retrieval(dataset, self.workspace)

    def test_invalid_fields_paths_labels_and_bounds_are_rejected(self):
        evaluate_retrieval, _ = api()
        mutations = [
            lambda d: d.update(unexpected=True),
            lambda d: d.update(version=True),
            lambda d: d.update(cases=[]),
            lambda d: d.update(cases=d["cases"] * 51),
            lambda d: d["cases"][0].update(answerable=1),
            lambda d: d["cases"][0].update(query=""),
            lambda d: d["cases"][0].update(extra="ignored"),
            lambda d: d["cases"][1].update(id="both"),
            lambda d: d["cases"][0]["relevant_spans"].append(self.span("a.py")),
            lambda d: d["cases"][0]["relevant_spans"][0].update(start_line=True),
            lambda d: d["cases"][0]["relevant_spans"][0].update(end_line=3),
            lambda d: d["cases"][0]["relevant_spans"][0].update(path="unlisted.py"),
            lambda d: d["cases"][0].update(relevant_spans=[]),
            lambda d: d["cases"][2].update(relevant_spans=[self.span("a.py")]),
            lambda d: d["source_files"].update({"../outside.py": "a" * 64}),
            lambda d: d["source_files"].update({".env": "a" * 64}),
            lambda d: d["source_files"].update({"README.md": "a" * 64}),
        ]
        for mutation in mutations:
            with self.subTest(mutation=mutations.index(mutation)):
                dataset = copy.deepcopy(self.dataset)
                mutation(dataset)
                with self.assertRaises(PolicyError):
                    evaluate_retrieval(dataset, self.workspace)
        for k in (True, 0, 21, 1.5):
            with self.subTest(k=k), self.assertRaises(PolicyError):
                evaluate_retrieval(self.dataset, self.workspace, k=k)

    def test_duplicate_and_overlapping_hits_do_not_inflate_recall(self):
        from paicli.evaluation import _score_case
        hit = {"path": "a.py", "start_line": 1, "end_line": 2,
               "symbol": "beacon", "content": self.sources["a.py"], "score": -1.0}
        overlap = dict(hit, end_line=1)
        row = _score_case(self.dataset["cases"][0], [hit, hit, overlap], 0.1)
        self.assertEqual(row["matched_relevant_spans"], 1)
        self.assertEqual(row["recall_at_k"], .5)
        self.assertEqual(row["reciprocal_rank"], 1.0)
        self.assertEqual(len(row["hits"]), 2)

    def test_graph_expansion_interleaves_resolved_callee_without_using_labels(self):
        evaluate_retrieval, _ = api()
        code = "from worker import perform\n\ndef gateway():\n    return perform()\n\ndef gateway_other():\n    return 0\n"
        worker = "def perform():\n    return 42\n"
        (self.root / "entry.py").write_text(code, encoding="utf-8", newline="")
        (self.root / "worker.py").write_text(worker, encoding="utf-8", newline="")
        dataset = copy.deepcopy(self.dataset)
        dataset["source_files"] = {path: hashlib.sha256(text.encode()).hexdigest()
                                   for path, text in {"entry.py": code, "worker.py": worker}.items()}
        dataset["cases"] = [self.case("callee", "gateway", True, [self.span("worker.py")])]
        report = evaluate_retrieval(dataset, self.workspace, k=2)
        baseline = report["methods"]["bm25"]["cases"][0]
        expanded = report["methods"]["graph_expand"]["cases"][0]
        self.assertEqual(baseline["recall_at_k"], 0)
        self.assertEqual(expanded["recall_at_k"], 1)
        self.assertEqual(expanded["reciprocal_rank"], .5)
        self.assertEqual(expanded["hits"][1]["path"], "worker.py")
        self.assertLessEqual(len(expanded["hits"]), 2)
        dataset["cases"][0]["relevant_spans"] = [self.span("entry.py", 6, 7)]
        changed = evaluate_retrieval(dataset, self.workspace, k=2)
        self.assertEqual(expanded["hits"], changed["methods"]["graph_expand"]["cases"][0]["hits"])

    def test_graph_expansion_excludes_whole_module_callers(self):
        from paicli.codegraph import CodeGraph
        from paicli.evaluation import _expand_hits
        contents = {"worker.py": "def perform():\n    return 42\n\nconstant = 1\n",
                    "entry.py": "from worker import perform\nperform()\nunrelated = 'irrelevant'\n"}
        for path, text in contents.items():
            (self.root / path).write_text(text, encoding="utf-8", newline="")
        graph = CodeGraph(self.workspace)
        graph.build()
        seed = {"path": "worker.py", "start_line": 1, "end_line": 2, "symbol": "perform",
                "content": "def perform():\n    return 42\n", "score": -1.0}
        hits = _expand_hits([seed], graph, contents, 5)
        self.assertEqual(hits, [seed])

    def test_graph_tail_label_outside_visible_complete_lines_does_not_count(self):
        evaluate_retrieval, _ = api()
        lines = ["def uniqueentry():\n", "    return target()\n", "\n", "def target():\n"]
        lines += ["    #" + "x" * 100 + "\n" for _ in range(110)]
        lines += ["    return 'tail_evidence_marker'\n"]
        source = "".join(lines)
        (self.root / "large.py").write_text(source, encoding="utf-8", newline="")
        dataset = copy.deepcopy(self.dataset)
        dataset["source_files"] = {"large.py": hashlib.sha256(source.encode()).hexdigest()}
        dataset["cases"] = [self.case("tail", "uniqueentry", True,
                                      [self.span("large.py", 115, 115)])]
        report = evaluate_retrieval(dataset, self.workspace)
        row = report["methods"]["graph_expand"]["cases"][0]
        self.assertEqual(row["recall_at_k"], 0)
        self.assertEqual(row["reciprocal_rank"], 0)
        expanded = [hit for hit in row["hits"] if hit.get("via") == "resolved_one_hop"]
        self.assertEqual(len(expanded), 1)
        self.assertLess(expanded[0]["end_line"], 115)
        for hit in row["hits"]:
            self.assertEqual(hit["content"], "".join(lines[hit["start_line"] - 1:hit["end_line"]]))
            self.assertLessEqual(len(hit["content"]), 8000)
            self.assertNotIn("tail_evidence_marker", hit["content"])

    def test_lexical_tail_label_outside_visible_complete_lines_does_not_count(self):
        evaluate_retrieval, _ = api()
        lines = ["def uniquelexicalentry():\n"]
        lines += ["    #" + "x" * 100 + "\n" for _ in range(90)]
        lines += ["    return 'tail_evidence_marker'\n"]
        source = "".join(lines)
        (self.root / "large.py").write_text(source, encoding="utf-8", newline="")
        dataset = copy.deepcopy(self.dataset)
        dataset["source_files"] = {"large.py": hashlib.sha256(source.encode()).hexdigest()}
        dataset["cases"] = [self.case("tail", "uniquelexicalentry", True,
                                      [self.span("large.py", 92, 92)])]
        report = evaluate_retrieval(dataset, self.workspace)
        for method in report["methods"].values():
            row = method["cases"][0]
            self.assertEqual(row["recall_at_k"], 0)
            self.assertEqual(len(row["hits"]), 1)
            hit = row["hits"][0]
            self.assertLess(hit["end_line"], 92)
            self.assertEqual(hit["content"], "".join(lines[hit["start_line"] - 1:hit["end_line"]]))
            self.assertLessEqual(len(hit["content"]), 8000)

    def test_exact_crlf_character_budget_keeps_only_complete_visible_line(self):
        evaluate_retrieval, _ = api()
        first = "#" + "x" * 7997 + "\r\n"
        source = first + "boundary_marker = 1\r\n"
        self.assertEqual(len(first), 8000)
        (self.root / "boundary.py").write_text(source, encoding="utf-8", newline="")
        dataset = copy.deepcopy(self.dataset)
        dataset["source_files"] = {"boundary.py": hashlib.sha256(source.encode()).hexdigest()}
        dataset["cases"] = [self.case("boundary", "boundary_marker", True,
                                      [self.span("boundary.py", 2, 2)])]
        report = evaluate_retrieval(dataset, self.workspace)
        for name in ("bm25", "graph_expand"):
            row = report["methods"][name]["cases"][0]
            self.assertEqual(row["recall_at_k"], 0)
            self.assertEqual([(hit["start_line"], hit["end_line"], hit["content"]) for hit in row["hits"]],
                             [(1, 1, first)])
        # Code-aware retrieval makes a separate complete window for line 2;
        # its label counts only because that exact source line is displayed.
        for name in ("code_aware", "code_aware_gated"):
            row = report["methods"][name]["cases"][0]
            self.assertEqual(row["recall_at_k"], 1)
            self.assertEqual([(hit["start_line"], hit["end_line"], hit["content"]) for hit in row["hits"]],
                             [(2, 2, "boundary_marker = 1\r\n")])

    def test_oversized_first_line_is_dropped_instead_of_claiming_partial_line(self):
        evaluate_retrieval, _ = api()
        source = "#" + "x" * 7998 + "\r\n" + "boundary_marker = 1\r\n"
        (self.root / "boundary.py").write_text(source, encoding="utf-8", newline="")
        dataset = copy.deepcopy(self.dataset)
        dataset["source_files"] = {"boundary.py": hashlib.sha256(source.encode()).hexdigest()}
        dataset["cases"] = [self.case("boundary", "boundary_marker", True,
                                      [self.span("boundary.py", 1, 2)])]
        report = evaluate_retrieval(dataset, self.workspace)
        for name in ("bm25", "graph_expand"):
            row = report["methods"][name]["cases"][0]
            self.assertEqual(row["recall_at_k"], 0)
            self.assertEqual(row["hits"], [])
        # Omitting the oversized line does not remove a later complete window.
        for name in ("code_aware", "code_aware_gated"):
            row = report["methods"][name]["cases"][0]
            self.assertEqual(row["recall_at_k"], 1)
            self.assertEqual([(hit["start_line"], hit["end_line"], hit["content"]) for hit in row["hits"]],
                             [(2, 2, "boundary_marker = 1\r\n")])


@asynccontextmanager
async def local_model_endpoint(responses):
    """Real loopback HTTP endpoint; never leaves the machine or uses credentials."""
    requests = []
    tasks = set()

    async def handler(reader, writer):
        tasks.add(asyncio.current_task())
        try:
            header = await reader.readuntil(b"\r\n\r\n")
            length = next(int(line.split(b":", 1)[1]) for line in header.split(b"\r\n")
                          if line.lower().startswith(b"content-length:"))
            requests.append(json.loads(await reader.readexactly(length)))
            response = responses[len(requests) - 1]
            if response == "slow":
                await asyncio.sleep(2)
                response = {"choices": []}
            body = json.dumps(response).encode()
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
                         + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body)
            await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
            await writer.wait_closed()
            tasks.discard(asyncio.current_task())

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    try:
        yield f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/v1", requests
    finally:
        server.close()
        await server.wait_closed()
        pending = list(tasks)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)


class AnswerEvaluationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        source = "def beacon():\n    return 1\n"
        (root / "source.py").write_text(source, encoding="utf-8", newline="")
        self.dataset = {
            "name": "answer_protocol_fixture", "version": 1,
            "annotation_note": "ANNOTATION_MUST_NOT_ENTER_PROMPT",
            "source_files": {"source.py": hashlib.sha256(source.encode()).hexdigest()},
            "cases": [RetrievalEvaluationTests.case("positive", "beacon", True,
                                                   [RetrievalEvaluationTests.span("source.py")]),
                      RetrievalEvaluationTests.case("negative", "quartzunknown", False, [])],
        }
        self.workspace = Workspace(root)

    @staticmethod
    def reply(answer="It returns one.", abstain=False, citations=None):
        return {"role": "assistant", "content": json.dumps({"answer": answer, "abstain": abstain,
                "citations": ["E1"] if citations is None else citations})}

    async def test_fixed_evidence_excludes_labels_and_repeats_preserve_denominators(self):
        retrieval, answers = api()
        report = retrieval(self.dataset, self.workspace)
        model = ScriptedModel([self.reply(), self.reply(),
                               self.reply("", True, []), self.reply("", True, [])])
        result = await answers(self.dataset, report, model, repeats=2)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(result["cases"]), 4)
        self.assertEqual(result["metrics"]["total_attempts"], 4)
        self.assertEqual(result["metrics"]["format_valid_count"], 4)
        self.assertEqual(result["metrics"]["citation_valid_count"], 4)
        self.assertEqual(result["metrics"]["answerable_attempts"], 2)
        self.assertEqual(result["metrics"]["unanswerable_attempts"], 2)
        self.assertEqual(result["metrics"]["answerable_abstention_rate"], 0)
        self.assertEqual(result["metrics"]["unanswerable_abstention_rate"], 1)
        self.assertIsNone(result["usage"])
        self.assertIsNone(result["cost"])
        self.assertEqual(result["semantic_accuracy"]["status"], "not_evaluated")
        self.assertEqual(result["semantic_accuracy"]["human_review"], "pending")
        request_text = json.dumps(model.requests, ensure_ascii=False)
        self.assertNotIn("ANNOTATION_MUST_NOT_ENTER_PROMPT", request_text)
        self.assertNotIn("relevant_spans", request_text)
        self.assertNotIn("answerable", request_text)
        self.assertIn("untrusted", request_text)
        self.assertEqual(model.requests[0], model.requests[1])
        supplied = json.loads(model.requests[0][1]["content"])
        self.assertEqual(supplied["evidence"][0]["id"], "E1")
        self.assertIn("return 1", supplied["evidence"][0]["content"])

    async def test_answer_evaluation_accepts_both_code_aware_methods(self):
        retrieval, answers = api()
        report = retrieval(self.dataset, self.workspace)
        for method in ("code_aware", "code_aware_gated"):
            with self.subTest(method=method):
                model = ScriptedModel([self.reply(), self.reply("", True, [])])
                result = await answers(self.dataset, report, model, method=method)
                self.assertEqual(result["status"], "completed")
                self.assertEqual(result["method"], method)
                self.assertEqual(result["metrics"]["total_attempts"], 2)

    async def test_gated_answer_prompt_receives_no_rejected_raw_candidates(self):
        retrieval, answers = api()
        dataset = copy.deepcopy(self.dataset)
        dataset["cases"] = [dataset["cases"][0]]
        dataset["cases"][0]["query"] = "beacon quartzmissing zirconmissing"
        report = retrieval(dataset, self.workspace)
        self.assertTrue(report["methods"]["code_aware"]["cases"][0]["hits"])
        model = ScriptedModel([self.reply("", True, [])])
        result = await answers(dataset, report, model, method="code_aware_gated")
        self.assertEqual(result["cases"][0]["evidence"], [])
        self.assertEqual(json.loads(model.requests[0][1]["content"])["evidence"], [])
        self.assertEqual(result["metrics"]["answerable_abstention_rate"], 1)

    async def test_bad_json_protocol_and_citations_remain_per_attempt_failures(self):
        retrieval, answers = api()
        dataset = copy.deepcopy(self.dataset)
        dataset["cases"] = [dataset["cases"][0]]
        report = retrieval(dataset, self.workspace)
        invalids = [
            ({"role": "assistant", "content": "not-json"}, "format_error"),
            ({"role": "user", "content": "{}"}, "protocol_error"),
            ({"role": "assistant", "content": "{}", "tool_calls": [{"id": "call"}]}, "protocol_error"),
            (self.reply(citations=["E99"]), "invalid_citations"),
            (self.reply(citations=["E1", "E1"]), "invalid_citations"),
            ({"role": "assistant", "content": '{"answer":"ok","abstain":false,"abstain":true,"citations":[]}'}, "format_error"),
            ({"role": "assistant", "content": '{"answer":"ok","abstain":0,"citations":[]}'}, "format_error"),
            ({"role": "assistant", "content": '{"answer":"ok","abstain":false,"citations":[],"extra":1}'}, "format_error"),
        ]
        for reply, expected in invalids:
            with self.subTest(status=expected, reply=reply):
                result = await answers(dataset, report, ScriptedModel([reply]))
                self.assertEqual(result["status"], "completed_with_errors")
                self.assertEqual(result["cases"][0]["status"], expected)
                self.assertEqual(result["metrics"]["total_attempts"], 1)
                self.assertEqual(result["metrics"]["failure_count"], 1)

    async def test_report_mismatch_and_unbounded_parameters_are_rejected(self):
        retrieval, answers = api()
        report = retrieval(self.dataset, self.workspace)
        for kwargs in ({"repeats": 0}, {"repeats": 4}, {"repeats": True},
                       {"timeout_seconds": 0}, {"timeout_seconds": 121}, {"timeout_seconds": float("nan")},
                       {"timeout_seconds": True}, {"method": "oracle"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(PolicyError):
                await answers(self.dataset, report, ScriptedModel([]), **kwargs)
        changed = copy.deepcopy(self.dataset)
        changed["cases"][0]["query"] = "changed query"
        with self.assertRaises(PolicyError):
            await answers(changed, report, ScriptedModel([]))
        changed_report = copy.deepcopy(report)
        changed_report["methods"]["bm25"]["cases"].pop()
        with self.assertRaises(PolicyError):
            await answers(self.dataset, changed_report, ScriptedModel([]))

    async def test_real_loopback_model_covers_success_invalid_reference_protocol_failure_and_timeout(self):
        retrieval, answers = api()
        dataset = copy.deepcopy(self.dataset)
        dataset["cases"] = [dict(dataset["cases"][0], id=f"case{i}") for i in range(4)]
        report = retrieval(dataset, self.workspace)
        responses = [{"choices": [{"message": self.reply()}]},
                     {"choices": [{"message": self.reply(citations=["E99"])}]},
                     {"invalid_completion": "response body must not be copied"}, "slow"]
        async with local_model_endpoint(responses) as (endpoint, requests):
            async with await asyncio.to_thread(httpx.AsyncClient, trust_env=False) as client:
                model = OpenAIModel(endpoint, "local-fixture", "placeholder", client=client)
                result = await answers(dataset, report, model, timeout_seconds=1)
        self.assertEqual([row["status"] for row in result["cases"]],
                         ["ok", "invalid_citations", "model_error", "timeout"])
        self.assertEqual(result["metrics"]["total_attempts"], 4)
        self.assertEqual(result["metrics"]["failure_count"], 3)
        self.assertNotIn("response body must not be copied", json.dumps(result))
        self.assertTrue(all("tools" not in request for request in requests))


if __name__ == "__main__":
    unittest.main()
