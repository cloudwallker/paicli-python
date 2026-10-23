from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from paicli.errors import PolicyError
from paicli.workspace import Workspace

if importlib.util.find_spec("paicli.codegraph"):
    from paicli.codegraph import CodeGraph
else:
    CodeGraph = None


class CodeGraphTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(CodeGraph, "Cross-file graph implementation is missing")
        self.temporary = tempfile.TemporaryDirectory(prefix="paicli-graph-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def write(self, path, source):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.encode("utf-8"))

    def graph(self, sources):
        for path, source in sources.items():
            self.write(path, source)
        graph = CodeGraph(Workspace(self.root))
        graph.build()
        return graph

    def symbol(self, graph, path, qualname):
        return next(s for s in graph.symbols if s["path"] == path and s["qualname"] == qualname)

    def resolved(self, graph):
        names = {s["id"]: (s["path"], s["qualname"]) for s in graph.symbols}
        return {(names[e["caller"]], names[e["callee"]]) for e in graph.edges if e["status"] == "resolved"}

    def test_cross_file_alias_and_relative_import_have_source_evidence(self):
        source = "from .worker import work as run\nimport pkg.worker as worker\ndef launch():\n    run()\n    worker.work()\n"
        graph = self.graph({"pkg/__init__.py": "", "pkg/worker.py": "def work():\n    return 1\n", "pkg/api.py": source})
        self.assertEqual(self.resolved(graph), {(("pkg/api.py", "launch"), ("pkg/worker.py", "work"))})
        self.assertEqual([e["line"] for e in graph.edges], [4, 5])
        symbol = self.symbol(graph, "pkg/api.py", "launch")
        self.assertEqual(symbol["digest"], hashlib.sha256(source.encode()).hexdigest())
        self.assertEqual((symbol["start_line"], symbol["end_line"]), (3, 5))
        json.dumps(graph.build())

    def test_forward_calls_and_nested_scopes_resolve_without_importing_code(self):
        graph = self.graph({"app.py": "raise RuntimeError('must not execute')\ndef outer():\n    def inner():\n        return target()\n    return inner()\ndef target():\n    return 1\n"})
        edges = self.resolved(graph)
        self.assertIn((("app.py", "outer.inner"), ("app.py", "target")), edges)
        self.assertIn((("app.py", "outer"), ("app.py", "outer.inner")), edges)

    def test_deep_attribute_chain_is_diagnosed_without_crashing(self):
        graph = self.graph({'deep.py': 'a' + '.a' * 1500 + '()\n'})
        self.assertTrue(graph.diagnostics)
        self.assertTrue(all(e['status'] == 'unresolved' for e in graph.edges))

    def test_nested_class_bare_name_skips_outer_class_namespace(self):
        graph = self.graph({'app.py': 'def target():\n    pass\nclass Outer:\n    def target():\n        pass\n    class Inner:\n        target()\n'})
        self.assertEqual(self.resolved(graph), {(("app.py", "Outer.Inner"), ("app.py", "target"))})

    @unittest.skipUnless(sys.version_info >= (3, 12), 'PEP 695 syntax requires Python 3.12')
    def test_type_parameters_do_not_resolve_to_global_functions(self):
        graph = self.graph({'app.py': 'def target():\n    pass\ndef run[target]():\n    target()\nclass Worker[target]:\n    seen = [target() for item in (1,)]\n    deferred = lambda: target()\n    def run(self):\n        target()\n'})
        self.assertEqual(self.resolved(graph), set())

    def test_explicit_dynamic_namespace_writes_invalidate_declarations(self):
        graph = self.graph({'app.py': 'class Worker:\n    def work(self):\n        pass\n    def run(self):\n        self.work()\nsetattr(Worker, "work", replacement)\n',
                            'other.py': 'def work():\n    pass\nglobals()["work"] = replacement\nwork()\n'})
        self.assertEqual(self.resolved(graph), set())

    def test_plain_self_method_resolves_but_inherited_and_dynamic_instances_do_not(self):
        graph = self.graph({"app.py": "class Worker:\n    def run(self):\n        return self.work()\n    def work(self):\n        return 1\nclass Child(Worker):\n    def run(self):\n        return self.work()\ndef dispatch(obj):\n    return obj.work()\n"})
        self.assertEqual(self.resolved(graph), {(("app.py", "Worker.run"), ("app.py", "Worker.work"))})
        self.assertEqual(len([e for e in graph.edges if e["status"] == "unresolved"]), 2)

    def test_parameter_and_assignment_shadowing_never_guess_import_target(self):
        graph = self.graph({"dep.py": "def work():\n    pass\n", "app.py": "from dep import work\ndef first(work):\n    work()\ndef second():\n    work()\n    work = lambda: None\ndef third():\n    work = other\n    work()\n"})
        self.assertEqual(self.resolved(graph), set())
        self.assertEqual(len(graph.edges), 3)
        self.assertTrue(all(e["reason"] and e["callee"] is None for e in graph.edges))

    def test_module_rebinding_and_conditional_import_are_unknown(self):
        graph = self.graph({"dep.py": "def work():\n    pass\n", "app.py": "from dep import work\nwork = replacement\nif flag:\n    from dep import work as maybe\ndef run():\n    work()\n    maybe()\n"})
        self.assertEqual(self.resolved(graph), set())

    def test_star_import_is_not_expanded_and_external_module_is_unknown(self):
        graph = self.graph({"dep.py": "def work():\n    pass\n", "app.py": "from dep import *\nimport os\ndef run():\n    work()\n    os.getcwd()\n"})
        self.assertEqual(self.resolved(graph), set())
        self.assertEqual(len(graph.edges), 2)

    def test_lambda_and_comprehension_bindings_do_not_capture_global_function(self):
        graph = self.graph({"app.py": "def work():\n    pass\ndef run(items):\n    f = lambda work: work()\n    return [work() for work in items]\n"})
        self.assertEqual(self.resolved(graph), set())
        self.assertEqual(len(graph.edges), 2)

    def test_decorated_targets_and_monkey_patched_methods_are_unknown(self):
        graph = self.graph({"app.py": "@replacement\ndef target():\n    pass\nclass Worker:\n    def run(self):\n        return self.work()\n    def work(self):\n        pass\n    def patch(self):\n        self.work = replacement\ndef caller():\n    target()\n"})
        self.assertEqual(self.resolved(graph), set())

    def test_cycle_neighbors_are_bounded_and_include_unresolved_outgoing_calls(self):
        graph = self.graph({"app.py": "def first():\n    second()\n    unknown()\ndef second():\n    first()\n"})
        first = self.symbol(graph, "app.py", "first")
        result = graph.neighbors(first["id"], depth=4)
        self.assertEqual({s["qualname"] for s in result["symbols"]}, {"second"})
        self.assertEqual(len(result["edges"]), 2)
        self.assertEqual(len(result["unresolved"]), 1)
        callers = graph.neighbors(first["id"], direction="callers")
        self.assertEqual({s["qualname"] for s in callers["symbols"]}, {"second"})
        self.assertFalse(result["truncated"])

    def test_find_symbols_and_neighbor_limit_report_truncation(self):
        graph = self.graph({"app.py": "def run():\n    work_one()\n    work_two()\ndef work_one():\n    pass\ndef work_two():\n    pass\n"})
        result = graph.find_symbols("work", limit=1)
        self.assertEqual(len(result["symbols"]), 1)
        self.assertTrue(result["truncated"])
        run = self.symbol(graph, "app.py", "run")
        result = graph.neighbors(run["id"], limit=1)
        self.assertEqual(len(result["symbols"]), 1)
        self.assertTrue(result["truncated"])

    def test_query_rejects_invalid_options_and_unknown_symbol(self):
        graph = self.graph({"a.py": "def f():\n    pass\n"})
        for kwargs in ({"query": ""}, {"query": "x", "limit": True}, {"query": "x", "limit": 0}):
            with self.assertRaises(PolicyError):
                graph.find_symbols(**kwargs)
        sid = self.symbol(graph, "a.py", "f")["id"]
        for kwargs in ({"symbol_id": "missing"}, {"symbol_id": sid, "direction": "sideways"}, {"symbol_id": sid, "depth": 0}, {"symbol_id": sid, "depth": True}):
            with self.assertRaises(PolicyError):
                graph.neighbors(**kwargs)

    def test_rebuild_discards_deleted_files_and_changed_edges(self):
        graph = self.graph({"a.py": "from b import target\ndef run():\n    target()\n", "b.py": "def target():\n    pass\n"})
        self.assertEqual(len(self.resolved(graph)), 1)
        (self.root / "b.py").unlink()
        graph.build()
        self.assertEqual(self.resolved(graph), set())
        self.assertFalse(any(s["path"] == "b.py" for s in graph.symbols))
        self.write("a.py", "def other():\n    pass\n")
        graph.build()
        self.assertEqual(graph.edges, [])
        self.assertFalse(graph.find_symbols("run")["symbols"])

    def test_syntax_error_protected_path_and_oversize_are_reported_without_execution(self):
        graph = self.graph({"good.py": "def okay():\n    pass\n", "bad.py": "def :\n", ".env.py": "def secret():\n    pass\n", "large.py": "#" * (512 * 1024 + 1)})
        report = graph.build()
        self.assertFalse(any(s["path"] in {"bad.py", ".env.py", "large.py"} for s in graph.symbols))
        self.assertTrue(any(d["path"] == "bad.py" and d["reason"] == "syntax_error" for d in report["diagnostics"]))
        self.assertTrue(any(d["path"] == "large.py" for d in report["diagnostics"]))
        self.assertNotIn("secret", json.dumps(report))

    def test_symbol_and_edge_budgets_report_incomplete_graph(self):
        self.write("app.py", "def a():\n    b()\n    b()\ndef b():\n    pass\n")
        with patch("paicli.codegraph.MAX_SYMBOLS", 2):
            graph = CodeGraph(Workspace(self.root))
            report = graph.build()
            self.assertLessEqual(len(graph.symbols), 2)
            self.assertTrue(report["truncated"])
        with patch("paicli.codegraph.MAX_EDGES", 1):
            graph = CodeGraph(Workspace(self.root))
            report = graph.build()
            self.assertEqual(len(graph.edges), 1)
            self.assertTrue(report["truncated"])

    def test_source_byte_and_file_budgets_are_visible(self):
        self.write("a.py", "def a():\n    pass\n")
        self.write("b.py", "def b():\n    pass\n")
        with patch("paicli.codegraph.MAX_SOURCE_BYTES", 20):
            graph = CodeGraph(Workspace(self.root))
            report = graph.build()
            self.assertEqual({s["path"] for s in graph.symbols}, {"a.py"})
            self.assertTrue(report["truncated"])
            self.assertLessEqual(report["source_bytes"], 20)
        with patch("paicli.codegraph.MAX_FILES", 1):
            graph = CodeGraph(Workspace(self.root))
            self.assertTrue(graph.build()["truncated"])
            self.assertEqual({s["path"] for s in graph.symbols}, {"a.py"})

    def test_module_reexports_and_unaliased_namespace_imports(self):
        graph = self.graph({"pkg/__init__.py": "from .worker import work\n", "pkg/worker.py": "def work():\n    pass\n", "app.py": "import pkg.worker\nfrom pkg import work\ndef run():\n    pkg.worker.work()\n    work()\n"})
        self.assertEqual(self.resolved(graph), {(("app.py", "run"), ("pkg/worker.py", "work"))})
        self.assertEqual(sum(e["status"] == "resolved" for e in graph.edges), 2)

    def test_import_reexport_cycles_do_not_recurse_forever(self):
        graph = self.graph({"a.py": "from b import work\ndef run():\n    work()\n", "b.py": "from a import work\n"})
        self.assertEqual(self.resolved(graph), set())
        self.assertEqual(graph.edges[0]["status"], "unresolved")

    def test_module_lines_do_not_count_unicode_string_separators(self):
        graph = self.graph({"app.py": "TEXT = 'a\u2028b'\ndef run():\n    pass\n"})
        module = self.symbol(graph, "app.py", "<module>")
        self.assertEqual(module["end_line"], 3)

    def test_global_nonlocal_and_comprehension_writes_invalidate_bindings(self):
        graph = self.graph({"dep.py": "def work():\n    pass\n", "app.py": "from dep import work\ndef change():\n    global work\n    work = replacement\ndef run():\n    work()\ndef outer():\n    from dep import work as local\n    def change_local():\n        nonlocal local\n        local = replacement\n    local()\ndef comp(items):\n    [(work := item) for item in items]\n    work()\n"})
        self.assertEqual(self.resolved(graph), set())

    def test_self_called_as_an_instance_is_unknown(self):
        graph = self.graph({"app.py": "class Worker:\n    def run(self):\n        self()\n"})
        self.assertEqual(self.resolved(graph), set())

    def test_ambiguous_module_paths_do_not_choose_arbitrarily(self):
        graph = self.graph({"pkg.py": "def work():\n    pass\n", "pkg/__init__.py": "def work():\n    pass\n", "app.py": "from pkg import work\ndef run():\n    work()\n"})
        self.assertEqual(self.resolved(graph), set())

    def test_mapping_pattern_capture_is_a_local_binding(self):
        graph = self.graph({"dep.py": "def work():\n    pass\n", "app.py": "from dep import work\ndef run(value):\n    match value:\n        case {'value': item, **work}:\n            work()\n"})
        self.assertEqual(self.resolved(graph), set())

    def test_rebound_module_attribute_invalidates_import_path(self):
        graph = self.graph({"pkg/__init__.py": "", "pkg/worker.py": "def work():\n    pass\n", "app.py": "import pkg.worker\npkg.worker = replacement\ndef run():\n    pkg.worker.work()\n"})
        self.assertEqual(self.resolved(graph), set())


if __name__ == "__main__":
    unittest.main()
