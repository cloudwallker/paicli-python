"""Runnable capability fixtures protect retrieval from adjacent vocabulary."""
from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from paicli.index import CodeIndex
from paicli.retrieval import CodeRetriever
from paicli.storage import Store
from paicli.workspace import Workspace


LEASE = """def renew_task_lease(task, leases, heartbeat, now):
    if now - heartbeat[task] > 5:
        leases.pop(task, None)
        return False
    leases[task] = now + 10
    return True
"""

MIGRATION = """def apply_migration(state, version, migration):
    saved = dict(state)
    try:
        state.update(migration(state))
        state["version"] = version
    except ValueError:
        state.clear()
        state.update(saved)
        raise
    return state
"""

ALLOWLIST = """def path_is_allowed(path, user_allowlist, project_allowlist, protected):
    if path in protected:
        return False
    allowed = set(user_allowlist) | set(project_allowlist)
    return path in allowed
"""

ROUTING = """def route_request(request, providers, costs, latencies):
    scores = {}
    for provider in providers:
        scores[provider] = costs[provider] + latencies[provider]
    selected = min(scores, key=scores.get)
    return selected, request
"""


class CapabilityRetrievalTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.workspace = Workspace(self.root)
        self.store = Store(self.workspace)
        self.addCleanup(self.store.close)
        self.index = CodeIndex(self.workspace, self.store)

    def retrieve(self, source, query):
        (self.root / "sample.py").write_text(source, encoding="utf-8")
        self.index.build()
        return CodeRetriever(self.index).search(query)

    def run_fixture(self, source):
        namespace = {}
        exec(compile(source, "capability_fixture.py", "exec"), namespace)
        return namespace

    def assert_supported(self, source, query):
        result = self.retrieve(source, query)
        self.assertEqual(result["decision"]["status"], "accepted", result["decision"])
        self.assertEqual(result["hits"][0]["path"], "sample.py")
        return result

    def test_lease_and_heartbeat_queries_find_running_renewal_and_reclamation(self):
        renew = self.run_fixture(LEASE)["renew_task_lease"]
        leases = {"job": 1}
        self.assertTrue(renew("job", leases, {"job": 5}, 6))
        self.assertEqual(leases, {"job": 16})
        self.assertFalse(renew("job", leases, {"job": 1}, 7))
        self.assertEqual(leases, {})
        for query in ("How does task lease renewal use heartbeats?",
                      "任务租约如何使用心跳？"):
            with self.subTest(query=query):
                self.assert_supported(LEASE, query)

    def test_migration_queries_find_running_update_and_rollback(self):
        migrate = self.run_fixture(MIGRATION)["apply_migration"]
        state = {"version": 1, "old": "preserved"}
        self.assertEqual(migrate(state, 2, lambda _: {"new": "ok"}),
                         {"version": 2, "old": "preserved", "new": "ok"})

        def interrupted(current):
            current.clear()
            raise ValueError("interrupted")

        with self.assertRaises(ValueError):
            migrate(state, 3, interrupted)
        self.assertEqual(state, {"version": 2, "old": "preserved", "new": "ok"})
        for query in ("How does migration preserve older data on failure?", "迁移"):
            with self.subTest(query=query):
                self.assert_supported(MIGRATION, query)

    def test_allowlist_queries_find_a_running_merge_and_protected_path_denial(self):
        allowed = self.run_fixture(ALLOWLIST)["path_is_allowed"]
        self.assertTrue(allowed("a", ["a"], [], []))
        self.assertTrue(allowed("b", [], ["b"], []))
        self.assertFalse(allowed("c", ["a"], ["b"], []))
        self.assertFalse(allowed("private", ["private"], [], ["private"]))
        queries = (
            "How do path allowlists merge user and project rules?",
            "不同用户与项目的路径放行规则怎样合并生效？",
            "Does the path allowlist override protected paths?",
            "路径放行规则会不会覆盖受保护目录里的限制？",
            "白名单", "允许列表", "whitelist",
        )
        for query in queries:
            with self.subTest(query=query):
                self.assert_supported(ALLOWLIST, query)

    def test_routing_queries_find_running_cost_and_latency_selection(self):
        route = self.run_fixture(ROUTING)["route_request"]
        self.assertEqual(route("payload", ["a", "b"],
                               {"a": 2, "b": 1}, {"a": 1, "b": 0}), ("b", "payload"))
        for query in ("How does request routing choose a provider?",
                      "不同请求在路由过程中怎样依据成本和延迟选择目标？", "router", "routed"):
            with self.subTest(query=query):
                self.assert_supported(ROUTING, query)

    def test_generic_task_storage_path_and_model_code_cannot_prove_specific_capabilities(self):
        pairs = (
            ("def task_runner(task):\n    return task\n",
             "How does task execution renew leases after a worker heartbeat disappears?"),
            ("def store_session(session):\n    return dict(session)\n",
             "How does storage migration preserve older sessions during upgrade?"),
            ("def path_allowed(path, protected):\n    return path not in protected\n",
             "How do path allowlists merge user and project rules?"),
            ("def model_request(request):\n    return request\n",
             "How does model request routing choose providers from their live measurements?"),
        )
        for source, query in pairs:
            with self.subTest(query=query):
                self.assertEqual(self.retrieve(source, query)["hits"], [])

    def test_comments_strings_imports_parameters_and_stubs_supply_no_capability_evidence(self):
        decoys = (
            "def run():\n    # lease heartbeat migration allowlist routing\n    return 1\n",
            'def run():\n    return "lease heartbeat migration allowlist routing"\n',
            "from future import lease, heartbeat, migration, allowlist, routing\n",
            "def run(lease, heartbeat, migration, allowlist, routing):\n    return 1\n",
            "def lease_heartbeat_migration_allowlist_routing():\n    pass\n",
            "def lease_heartbeat_migration_allowlist_routing():\n    raise NotImplementedError()\n",
        )
        for source in decoys:
            for query in ("lease", "heartbeat", "migration", "allowlist", "routing"):
                with self.subTest(source=source, query=query):
                    self.assertEqual(self.retrieve(source, query)["hits"], [])

    def test_lease_and_heartbeat_cannot_be_assembled_across_function_scopes(self):
        source = ("class Worker:\n"
                  "    def lease(self):\n"
                  "        return 1\n"
                  "    def heartbeat(self):\n"
                  "        return 2\n")
        self.assertEqual(self.retrieve(source, "lease heartbeat")["hits"], [])
        self.assertEqual(self.retrieve("def lease():\n    return 1\n",
                                       "lease heartbeat")["hits"], [])

    def test_negated_question_requires_evidence_but_can_return_explicit_rejection(self):
        source = ("def route_request(request):\n"
                  '    raise ValueError("provider not supported")\n')
        route = self.run_fixture(source)["route_request"]
        with self.assertRaises(ValueError):
            route("payload")
        self.assert_supported(source, "Does request routing reject an unsupported provider?")
        result = self.retrieve("def task_runner(task):\n    return task\n",
                               "Does task execution support lease renewal and heartbeat reclamation?")
        self.assertEqual(result["hits"], [])

    def test_ordinary_path_permission_cost_and_retry_queries_keep_existing_policy(self):
        pairs = (
            ("def path_allowed(path, protected):\n    return path not in protected\n", "路径目录"),
            ("def permission_check(permission):\n    return bool(permission)\n", "权限"),
            ("def token_cost(token):\n    return len(token)\n", "token cost"),
            ("def validate_tool_call(call):\n"
             "    if call is None:\n"
             '        raise ValueError("invalid call; do not retry")\n'
             "    return call\n", "Does validation retry an invalid tool call?"),
        )
        for source, query in pairs:
            with self.subTest(query=query):
                result = self.assert_supported(source, query)
                self.assertEqual(result["query"]["required_behaviors"], [])


if __name__ == "__main__":
    unittest.main()
