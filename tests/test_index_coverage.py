from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from paicli.index import CodeIndex, chunks_for
from paicli.storage import Store
from paicli.workspace import Workspace


class IndexCoverageTests(unittest.TestCase):
    def test_python_chunks_cover_module_code_and_keep_decorators_and_nested_ranges(self):
        source = (
            "import unique_dependency\n"
            "PLATFORM_SENTINEL = 'module configuration'\n"
            "\n"
            "@registered(\n"
            "    'decorator sentinel'\n"
            ")\n"
            "class Client:\n"
            "    async def fetch(self):\n"
            "        def decode():\n"
            "            return 'nested sentinel'\n"
            "        return decode()\n"
            "\n"
            "if __name__ == '__main__':\n"
            "    print(PLATFORM_SENTINEL)\n"
        )
        chunks = chunks_for("client.py", source)
        lines = source.splitlines(keepends=True)
        covered = {line for _, start, stop, _ in chunks for line in range(start, stop + 1)}
        self.assertEqual(covered, set(range(1, 15)))
        for _, start, stop, content in chunks:
            self.assertEqual(content, "".join(lines[start - 1:stop]))
        by_symbol = {symbol: (start, stop, content) for symbol, start, stop, content in chunks}
        self.assertEqual(by_symbol["Client"][:2], (4, 11))
        self.assertEqual(by_symbol["fetch"][:2], (8, 11))
        self.assertEqual(by_symbol["decode"][:2], (9, 10))
        self.assertIn("@registered(", by_symbol["Client"][2])

    def test_large_uncovered_regions_stay_bounded_and_preserve_every_source_line(self):
        source = "".join(f"CONSTANT_{number} = {number}\n" for number in range(220)) + "def small():\n    return 1\n" + "END_SENTINEL = 99\n"
        chunks = chunks_for("large.py", source)
        covered = {line for _, start, stop, _ in chunks for line in range(start, stop + 1)}
        self.assertEqual(covered, set(range(1, 224)))
        self.assertTrue(all(stop - start + 1 <= 100 for _, start, stop, _ in chunks))

    def test_unicode_line_separators_inside_python_strings_do_not_shift_source_ranges(self):
        source = 'LABEL = "before\u2028after"\n\ndef work():\n    return LABEL\n'
        chunks = chunks_for("unicode.py", source)
        function = next(chunk for chunk in chunks if chunk[0] == "work")
        self.assertEqual(function[1:3], (3, 4))
        self.assertEqual(function[3], "def work():\n    return LABEL\n")

    def test_index_covers_frontend_modules_styles_and_html_as_text(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for suffix, text in [(".mjs", "export const moduleonlyneedle = true;"),
                                 (".cjs", "exports.commononlyneedle = true;"),
                                 (".css", ".styleonlyneedle { color: blue; }"),
                                 (".html", "<main>htmlonlyneedle</main>")]:
                (root / ("frontend" + suffix)).write_text(text, encoding="utf-8")
            workspace = Workspace(root)
            store = Store(workspace)
            try:
                index = CodeIndex(workspace, store)
                self.assertEqual(index.build()["files"], 4)
                for query, path in [("moduleonlyneedle", "frontend.mjs"), ("commononlyneedle", "frontend.cjs"),
                                    ("styleonlyneedle", "frontend.css"), ("htmlonlyneedle", "frontend.html")]:
                    with self.subTest(query=query):
                        self.assertEqual(index.search(query)[0]["path"], path)
            finally:
                store.close()

    def test_index_finds_python_module_symbols_and_jsx_component_without_stale_results(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "service.py").write_text("import searchable_dependency\nMAX_OBJECT_SENTINEL = 4096\n\ndef run():\n    return 1\n", encoding="utf-8")
            (root / "Console.jsx").write_text("export function Console() { return <main>jsxquotaonlyneedle</main>; }\n", encoding="utf-8")
            workspace = Workspace(root)
            store = Store(workspace)
            try:
                index = CodeIndex(workspace, store)
                self.assertEqual(index.build()["files"], 2)
                for query, path, line in [("searchable_dependency", "service.py", 1), ("MAX_OBJECT_SENTINEL", "service.py", 2), ("jsxquotaonlyneedle", "Console.jsx", 1)]:
                    with self.subTest(query=query):
                        hits = index.search(query)
                        self.assertTrue(hits)
                        self.assertEqual(hits[0]["path"], path)
                        self.assertLessEqual(hits[0]["start_line"], line)
                        self.assertGreaterEqual(hits[0]["end_line"], line)
                (root / "Console.jsx").write_text("export const changed = true;\n", encoding="utf-8")
                self.assertEqual(index.search("jsxquotaonlyneedle"), [])
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
