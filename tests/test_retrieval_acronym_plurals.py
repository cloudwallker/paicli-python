"""Natural acronym plurals retain concept evidence and explicit-name boundaries."""
from pathlib import Path
import tempfile
import unittest

from paicli.index import CodeIndex
from paicli.retrieval import CodeRetriever
from paicli.storage import Store
from paicli.workspace import Workspace


class AcronymPluralTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.workspace = Workspace(self.root)
        self.store = Store(self.workspace)
        self.addCleanup(self.store.close)
        self.index = CodeIndex(self.workspace, self.store)

    def index_source(self, source):
        (self.root / 'calls.py').write_text(source, encoding='utf-8')
        self.index.build()
        return CodeRetriever(self.index)

    def test_natural_ids_find_singular_plural_and_full_identifier_names(self):
        for variable in ('call_id', 'call_ids', 'identifier', 'callIDs', 'getIDsFromCall',
                         'IDsForCall', 'callIDsValue', 'call_IDs2', 'callIDs2'):
            source = (f'def validate_tool_calls({variable}):\n'
                      f'    return len(set({variable})) == len({variable})\n')
            namespace = {}
            exec(source, namespace)
            self.assertFalse(namespace['validate_tool_calls'](['a', 'a']))
            self.assertTrue(namespace['validate_tool_calls'](['a', 'b']))
            retriever = self.index_source(source)
            for query in ('How are repeated tool-call IDs validated?', 'IDs 校验'):
                with self.subTest(variable=variable, query=query):
                    result = retriever.search(query)
                    self.assertEqual(result['decision']['status'], 'accepted')
                    self.assertIn(variable, result['hits'][0]['content'])
                    self.assertEqual(result['query']['required_concepts'], ['identifier'])

    def test_other_concepts_cannot_compensate_for_missing_id_evidence(self):
        retriever = self.index_source('def validate_tool_calls(values):\n    return bool(values)\n')
        result = retriever.search('How are repeated tool-call IDs validated?')
        self.assertTrue(result['candidates'])
        self.assertGreaterEqual(result['candidates'][0]['evidence_coverage'], 0.6)
        self.assertEqual(result['hits'], [])
        self.assertEqual(result['decision'].get('missing_required_concepts', []), ['identifier'])

    def test_backticks_require_the_original_plural_spelling(self):
        retriever = self.index_source('def validate_tool_calls(call_id):\n    return bool(call_id)\n')
        for query in ('How are `IDs` validated for each tool call?', '`IDs` 校验'):
            with self.subTest(query=query):
                self.assertEqual(retriever.search(query)['hits'], [])
        exact = self.index_source('def validate_tool_calls(IDs):\n    return bool(IDs)\n')
        self.assertTrue(exact.search('How are `IDs` validated for each tool call?')['hits'])

    def test_compound_and_unknown_plural_names_remain_exact(self):
        retriever = self.index_source(
            'def validate_tool_calls(id):\n'
            '    user = id\n    missing = id\n    ztr = id\n'
            '    return bool(user and missing and ztr)\n')
        for name in ('user_IDs', 'MissingIDs', 'ZTRs', 'URLs'):
            with self.subTest(name=name):
                result = retriever.search(f'How are {name} validated for each tool call?')
                self.assertEqual(result['hits'], [])
                self.assertIn(name.casefold(), result['decision']['missing_anchors'])

    def test_identifier_aliases_vote_once_and_keep_explicit_precedence(self):
        retriever = self.index_source('def validate(identifier):\n    return bool(identifier)\n')
        result = retriever.search('How are IDs and identifiers validated?')
        self.assertEqual(result['decision']['status'], 'accepted')
        self.assertEqual([c['term'] for c in result['query']['concepts']], ['identifier', 'validation'])
        self.assertEqual(result['query']['required_concepts'], ['identifier'])
        self.assertEqual(retriever.search('How are IDs and `MissingIDs` validated?')['hits'], [])

    def test_required_concept_does_not_leak_to_later_queries(self):
        retriever = self.index_source('def validate_tool_calls(values):\n    return bool(values)\n')
        rejected = retriever.search('How are repeated tool-call IDs validated?')
        self.assertEqual(rejected['hits'], [])
        self.assertEqual(rejected['query'].get('required_concepts', []), ['identifier'])
        rejected['query']['required_concepts'].append('tampered')
        ordinary = retriever.search('How are tool calls validated?')
        self.assertEqual(ordinary['decision']['status'], 'accepted')
        self.assertEqual(ordinary['query']['required_concepts'], [])

    def test_missing_concept_decoys_do_not_hide_a_later_valid_candidate(self):
        source = ''.join(f'def validate_repeated_tool_calls_{number}(values):\n'
                         '    return bool(values)\n\n' for number in range(5))
        source += 'def validate_call_ids(ids):\n    return len(set(ids)) == len(ids)\n'
        retriever = self.index_source(source)
        result = retriever.search('How are repeated tool-call IDs validated?')
        self.assertEqual(len(result['candidates']), 5)
        self.assertTrue(all(hit.get('missing_required_concepts') == ['identifier']
                            for hit in result['candidates']))
        self.assertEqual(result['decision']['status'], 'accepted')
        self.assertEqual([hit['symbol'] for hit in result['hits']], ['validate_call_ids'])

    def test_known_acronym_plural_components_preserve_camel_case_source_recall(self):
        for acronym, variable, concept in (('DBs', 'callDBs', 'database'),
                                           ('ASTs', 'callASTs', 'syntax')):
            retriever = self.index_source(f'def validate_tool_calls({variable}):\n'
                                          f'    return bool({variable})\n')
            with self.subTest(acronym=acronym):
                result = retriever.search(f'How are repeated tool-call {acronym} validated?')
                self.assertEqual(result['decision']['status'], 'accepted')
                self.assertEqual(result['query']['required_concepts'], [concept])
                self.assertEqual(retriever.search(f'How does Missing{acronym} validate tool calls?')['hits'], [])


if __name__ == '__main__':
    unittest.main()
