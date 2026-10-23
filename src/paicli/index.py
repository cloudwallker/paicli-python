"""Python AST chunks and SQLite FTS5/BM25 lexical retrieval."""
from __future__ import annotations

import ast
import hashlib
from pathlib import Path
import re

from .errors import PolicyError
from .storage import Store
from .workspace import Workspace


MAX_FILE_BYTES = 512 * 1024
INDEX_SUFFIXES = {".py", ".txt", ".md", ".toml", ".yaml", ".yml", ".json", ".sh",
                  ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".css", ".html", ".java"}


def digest_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def chunks_for(path: str, content: str) -> list[tuple[str, int, int, str]]:
    # Python AST line numbers count CR/LF source lines. Unicode separators can
    # appear inside string literals and must not shift the reported ranges.
    lines = [line for line in re.findall(r"[^\r\n]*(?:\r\n|\r|\n|$)", content) if line]
    if not lines:
        return []
    if Path(path).suffix.lower() == ".py":
        try:
            tree = ast.parse(content)
        except SyntaxError:
            tree = None
        if tree is not None:
            nodes = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
            if nodes:
                result = []
                covered = [False] * len(lines)
                for node in sorted(nodes, key=lambda n: (n.lineno, n.end_lineno)):
                    start = min([node.lineno] + [d.lineno for d in node.decorator_list])
                    end = node.end_lineno or node.lineno
                    covered[start - 1:end] = [True] * (end - start + 1)
                    # Large functions/classes retain line ranges but are split into
                    # bounded windows. Both class and method chunks may overlap.
                    for offset in range(start - 1, end, 100):
                        stop = min(offset + 100, end)
                        result.append((node.name, offset + 1, stop, "".join(lines[offset:stop])))
                # Definitions do not cover module imports, constants, docstrings,
                # comments, or executable top-level statements. Index every gap
                # without folding unrelated module lines into a named symbol.
                offset = 0
                while offset < len(lines):
                    if covered[offset]:
                        offset += 1
                        continue
                    stop = offset
                    while stop < len(lines) and not covered[stop] and stop - offset < 100:
                        stop += 1
                    result.append(("<module>", offset + 1, stop, "".join(lines[offset:stop])))
                    offset = stop
                return sorted(result, key=lambda chunk: (chunk[1], chunk[2], chunk[0]))
    return [(Path(path).stem, i + 1, min(i + 100, len(lines)), "".join(lines[i:i + 100])) for i in range(0, len(lines), 100)]


class CodeIndex:
    def __init__(self, workspace: Workspace, store: Store):
        self.workspace = workspace
        self.store = store

    def build(self) -> dict:
        paths, truncated = self.workspace.files(limit=5000)
        records = []
        files = 0
        skipped = 0
        for name in paths:
            if Path(name).suffix.lower() not in INDEX_SUFFIXES:
                continue
            try:
                path = self.workspace.resolve(name)
                if path.stat().st_size > MAX_FILE_BYTES:
                    skipped += 1
                    continue
                raw = path.read_bytes()
                content = raw.decode("utf-8")
            except (OSError, UnicodeError, PolicyError):
                skipped += 1
                continue
            digest = hashlib.sha256(raw).hexdigest()
            for symbol, start, end, chunk in chunks_for(name, content):
                records.append((name, symbol, start, end, chunk, digest))
            files += 1
        con = self.store.connection
        with con:
            con.execute("DELETE FROM chunks")
            con.execute("DELETE FROM chunks_fts")
            for record in records:
                cursor = con.execute("INSERT INTO chunks(path,symbol,start_line,end_line,content,digest) VALUES (?,?,?,?,?,?)", record)
                con.execute("INSERT INTO chunks_fts(rowid,path,symbol,content) VALUES (?,?,?,?)", (cursor.lastrowid, record[0], record[1], record[4]))
        return {"files": files, "chunks": len(records), "skipped": skipped, "truncated": truncated, "retrieval": "SQLite FTS5 / BM25 lexical"}

    def search(self, query: str, limit: int = 5) -> list[dict]:
        if not isinstance(query, str) or not query.strip() or len(query) > 500:
            raise PolicyError("Search query must contain 1–500 characters.")
        if type(limit) is not int or not 1 <= limit <= 20:
            raise PolicyError("Search limit must be between 1 and 20.")
        tokens = re.findall(r"\w+", query, flags=re.UNICODE)
        if not tokens:
            return []
        # Never interpolate a user-provided FTS expression. unicode61 splits '_'.
        tokens = [piece for token in tokens for piece in token.split("_") if piece]
        if not tokens:
            return []
        match = " OR ".join('"' + token + '"' for token in tokens[:32])
        rows = self.store.connection.execute("""
            SELECT c.path,c.symbol,c.start_line,c.end_line,c.content,c.digest,bm25(chunks_fts,1.0,2.0,1.0)
            FROM chunks_fts JOIN chunks c ON c.id=chunks_fts.rowid
            WHERE chunks_fts MATCH ? ORDER BY bm25(chunks_fts,1.0,2.0,1.0) LIMIT 100
        """, (match,))
        hits = []
        hashes = {}
        for path, symbol, start, end, content, digest, score in rows:
            try:
                target = self.workspace.resolve(path)
                if path not in hashes:
                    hashes[path] = digest_file(target) if target.stat().st_size <= MAX_FILE_BYTES else None
                if hashes[path] != digest:
                    continue
            except (OSError, PolicyError):
                continue
            hits.append({"path": path, "symbol": symbol, "start_line": start, "end_line": end, "content": content[:8000], "score": float(score)})
            if len(hits) == limit:
                break
        return hits
