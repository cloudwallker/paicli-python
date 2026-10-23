"""Bounded, conservative Python source-reference graph; never imports source code.

Resolved edges describe statically identifiable declarations, not a proof of Python
runtime dispatch. Dynamic rebinding, inheritance and unresolved imports stay unknown.
A graph is a snapshot: rebuild it after source changes (CLI/tools build per request).
"""
from __future__ import annotations

import ast
from dataclasses import dataclass, field
import hashlib
from pathlib import PurePosixPath

from .errors import PolicyError
from .index import MAX_FILE_BYTES
from .workspace import Workspace


MAX_FILES = 5000
MAX_SOURCE_BYTES = 8 * 1024 * 1024
MAX_SYMBOLS = 10000
MAX_EDGES = 30000
MAX_AST_NODES = 100000


@dataclass(eq=False)
class _Scope:
    symbol: dict
    module: str
    parent: _Scope | None = None
    bindings: dict[str, list[tuple[str, object]]] = field(default_factory=dict)
    blocked: set[str] = field(default_factory=set)
    parameters: set[str] = field(default_factory=set)
    type_parameters: set[str] = field(default_factory=set)
    mutated_attributes: set[str] = field(default_factory=set)
    star: bool = False
    dynamic: bool = False
    decorated: bool = False
    inherited: bool = False
    self_name: str | None = None
    owner_class: _Scope | None = None

    def bind(self, name, kind, value, conditional=False):
        self.bindings.setdefault(name, []).append((kind, value))
        if conditional:
            self.blocked.add(name)


def _parts(node):
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        if len(parts) > 256:
            return None
        node = node.value
    return [node.id, *reversed(parts)] if isinstance(node, ast.Name) else None


class _Collector(ast.NodeVisitor):
    def __init__(self, graph, scope):
        self.graph, self.scope = graph, scope
        self.conditional = 0

    def _definition(self, node, kind):
        parent = self.scope
        # Decorators, bases and defaults run in the surrounding scope.
        for expr in node.decorator_list:
            self.visit(expr)
        if isinstance(node, ast.ClassDef):
            for expr in node.bases:
                self.visit(expr)
            for keyword in node.keywords:
                self.visit(keyword.value)
        else:
            for expr in [*node.args.defaults, *node.args.kw_defaults]:
                if expr is not None:
                    self.visit(expr)
        scope = self.graph._add_scope(node, parent, kind)
        if scope is None:
            parent.blocked.add(node.name)
            return
        parent.bind(node.name, "symbol", scope, bool(self.conditional))
        # PEP 695 names have their own annotation scope, visible inside the
        # definition and descendants. They must never bind a same-named global.
        scope.type_parameters = parent.type_parameters | {
            parameter.name for parameter in getattr(node, "type_params", ())}
        scope.blocked.update(scope.type_parameters)
        scope.decorated = bool(node.decorator_list)
        if kind == "class":
            scope.inherited = bool(node.bases or node.keywords)
        else:
            args = node.args
            scope.parameters = {a.arg for a in [*args.posonlyargs, *args.args, *args.kwonlyargs]}
            if args.vararg:
                scope.parameters.add(args.vararg.arg)
            if args.kwarg:
                scope.parameters.add(args.kwarg.arg)
            positional = [*args.posonlyargs, *args.args]
            if parent.symbol["kind"] == "class":
                scope.owner_class = parent
                if positional and positional[0].arg == "self" and not scope.decorated:
                    scope.self_name = "self"
        visitor = _Collector(self.graph, scope)
        for child in node.body:
            visitor.visit(child)

    def visit_FunctionDef(self, node):
        self._definition(node, "function")

    def visit_AsyncFunctionDef(self, node):
        self._definition(node, "async_function")

    def visit_ClassDef(self, node):
        self._definition(node, "class")

    def visit_Import(self, node):
        for alias in node.names:
            self.scope.bind(alias.asname or alias.name.split(".")[0], "import",
                            alias.name if alias.asname else alias.name.split(".")[0], bool(self.conditional))

    def visit_ImportFrom(self, node):
        module = node.module or ""
        if node.level:
            root = self.scope
            while root.parent is not None:
                root = root.parent
            package = root.module.split(".")
            if not root.symbol["path"].endswith("/__init__.py") and root.symbol["path"] != "__init__.py":
                package = package[:-1]
            up = node.level - 1
            if up >= len(package):
                module = "<invalid-relative-import>"
            else:
                module = ".".join(package[:len(package) - up] + ([module] if module else []))
        for alias in node.names:
            if alias.name == "*":
                self.scope.star = True
            else:
                self.scope.bind(alias.asname or alias.name, "import", ".".join(p for p in (module, alias.name) if p), bool(self.conditional))

    def visit_Name(self, node):
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            self.scope.blocked.add(node.id)

    def visit_Attribute(self, node):
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            parts = _parts(node)
            if parts:
                self.graph._mutations.append((self.scope, parts))
        self.generic_visit(node)

    def visit_Global(self, node):
        self.scope.blocked.update(node.names)
        root = self.scope
        while root.parent is not None:
            root = root.parent
        root.blocked.update(node.names)

    def visit_Nonlocal(self, node):
        self.scope.blocked.update(node.names)
        parent = self.scope.parent
        while parent is not None and parent.symbol["kind"] != "module":
            parent.blocked.update(node.names)
            parent = parent.parent

    def visit_ExceptHandler(self, node):
        if node.name:
            self.scope.blocked.add(node.name)
        self.generic_visit(node)

    def visit_MatchAs(self, node):
        if node.name:
            self.scope.blocked.add(node.name)
        self.generic_visit(node)

    visit_MatchStar = visit_MatchAs

    def visit_MatchMapping(self, node):
        if node.rest:
            self.scope.blocked.add(node.rest)
        self.generic_visit(node)

    def visit_Call(self, node):
        if isinstance(node.func, ast.Name) and node.func.id in {"exec", "eval"}:
            self.scope.dynamic = True
        if isinstance(node.func, ast.Name) and node.func.id in {"globals", "locals", "vars"}:
            # Namespace aliases permit later mutation that cannot be tracked here.
            target = self.scope
            if node.func.id == "globals":
                while target.parent is not None:
                    target = target.parent
            target.dynamic = True
        if isinstance(node.func, ast.Name) and node.func.id in {"setattr", "delattr"} and node.args:
            parts = _parts(node.args[0])
            if parts:
                name = node.args[1] if len(node.args) > 1 else None
                member = name.value if isinstance(name, ast.Constant) and isinstance(name.value, str) else None
                self.graph._mutations.append((self.scope, parts + [member]))
        if len(self.graph._calls) < MAX_EDGES:
            self.graph._calls.append((self.scope, node))
        else:
            self.graph._limit("edge_limit")
        self.generic_visit(node)

    def _conditional(self, node):
        self.conditional += 1
        self.generic_visit(node)
        self.conditional -= 1

    visit_If = _conditional
    visit_For = _conditional
    visit_AsyncFor = _conditional
    visit_While = _conditional
    visit_Try = _conditional
    visit_TryStar = _conditional
    visit_With = _conditional
    visit_AsyncWith = _conditional
    visit_Match = _conditional

    def visit_Lambda(self, node):
        for value in [*node.args.defaults, *node.args.kw_defaults]:
            if value is not None:
                self.visit(value)
        scope = _Scope(self.scope.symbol, self.scope.module, self.scope)
        scope.type_parameters = set(self.scope.type_parameters)
        scope.blocked.update(scope.type_parameters)
        args = node.args
        scope.parameters = {a.arg for a in [*args.posonlyargs, *args.args, *args.kwonlyargs]}
        scope.parameters.update(a.arg for a in (args.vararg, args.kwarg) if a)
        _Collector(self.graph, scope).visit(node.body)

    def _comprehension(self, node):
        scope = _Scope(self.scope.symbol, self.scope.module, self.scope)
        scope.type_parameters = set(self.scope.type_parameters)
        scope.blocked.update(scope.type_parameters)
        # Assignment expressions in comprehensions can rebind the containing scope.
        for item in ast.walk(node):
            if isinstance(item, ast.NamedExpr) and isinstance(item.target, ast.Name):
                self.scope.blocked.add(item.target.id)
        # Target names are deliberately treated as unknown throughout this small scope.
        for generator in node.generators:
            for target in ast.walk(generator.target):
                if isinstance(target, ast.Name):
                    scope.parameters.add(target.id)
        visitor = _Collector(self.graph, scope)
        for generator in node.generators:
            visitor.visit(generator.iter)
            for condition in generator.ifs:
                visitor.visit(condition)
        if isinstance(node, ast.DictComp):
            visitor.visit(node.key)
            visitor.visit(node.value)
        else:
            visitor.visit(node.elt)

    visit_ListComp = _comprehension
    visit_SetComp = _comprehension
    visit_DictComp = _comprehension
    visit_GeneratorExp = _comprehension


class CodeGraph:
    def __init__(self, workspace: Workspace):
        self.workspace = workspace
        self.symbols: list[dict] = []
        self.edges: list[dict] = []
        self._reset()

    def _reset(self):
        self.symbols = []
        self.edges = []
        self.diagnostics = []
        self.truncated = False
        self._limit_reasons = set()
        self._modules = {}
        self._scopes = {}
        self._calls = []
        self._mutations = []
        self._built = False

    def _limit(self, reason):
        self.truncated = True
        if reason not in self._limit_reasons:
            self._limit_reasons.add(reason)
            self.diagnostics.append({"path": None, "reason": reason})

    def _add_scope(self, node, parent, kind):
        if len(self.symbols) >= MAX_SYMBOLS:
            self._limit("symbol_limit")
            return None
        qualname = node.name if parent.symbol["kind"] == "module" else parent.symbol["qualname"] + "." + node.name
        start = min([node.lineno] + [d.lineno for d in node.decorator_list])
        symbol = {"id": f'{parent.symbol["path"]}:{qualname}:{node.lineno}',
                  "path": parent.symbol["path"], "name": node.name, "qualname": qualname,
                  "start_line": start, "end_line": node.end_lineno or node.lineno,
                  "kind": kind, "digest": parent.symbol["digest"]}
        scope = _Scope(symbol, parent.module, parent)
        self.symbols.append(symbol)
        self._scopes[symbol["id"]] = scope
        return scope

    def build(self) -> dict:
        """Replace the complete snapshot; errors and all resource truncation are visible."""
        self._reset()
        paths, truncated = self.workspace.files(limit=MAX_FILES)
        if truncated:
            self._limit("file_limit")
        total_bytes = 0
        parsed_files = 0
        for name in paths:
            if not name.endswith(".py"):
                continue
            try:
                path = self.workspace.resolve(name)
                if path.stat().st_size > MAX_FILE_BYTES:
                    self.diagnostics.append({"path": name, "reason": "file_too_large"})
                    continue
                with path.open("rb") as handle:
                    raw = handle.read(MAX_FILE_BYTES + 1)
                if len(raw) > MAX_FILE_BYTES:
                    self.diagnostics.append({"path": name, "reason": "file_too_large"})
                    continue
                if total_bytes + len(raw) > MAX_SOURCE_BYTES:
                    self._limit("source_byte_limit")
                    break
                total_bytes += len(raw)
                content = raw.decode("utf-8-sig")
                tree = ast.parse(content)
                if sum(1 for _ in ast.walk(tree)) > MAX_AST_NODES:
                    self.diagnostics.append({"path": name, "reason": "ast_node_limit"})
                    self.truncated = True
                    continue
            except SyntaxError:
                self.diagnostics.append({"path": name, "reason": "syntax_error"})
                continue
            except (UnicodeError, OSError, PolicyError, ValueError, RecursionError):
                self.diagnostics.append({"path": name, "reason": "unreadable_or_unsupported_source"})
                continue
            if len(self.symbols) >= MAX_SYMBOLS:
                self._limit("symbol_limit")
                break
            module_parts = list(PurePosixPath(name).with_suffix("").parts)
            if module_parts[0] == "src":
                module_parts = module_parts[1:]
            if module_parts and module_parts[-1] == "__init__":
                module_parts.pop()
            module_name = ".".join(module_parts)
            line_count = content.count("\n") + content.count("\r") - content.count("\r\n")
            if not content.endswith(("\n", "\r")):
                line_count += 1
            symbol = {"id": name + ":<module>:1", "path": name, "name": "<module>", "qualname": "<module>",
                      "start_line": 1, "end_line": max(1, line_count), "kind": "module", "digest": hashlib.sha256(raw).hexdigest()}
            scope = _Scope(symbol, module_name)
            self.symbols.append(symbol)
            self._scopes[symbol["id"]] = scope
            self._modules.setdefault(module_name, []).append(scope)
            try:
                _Collector(self, scope).visit(tree)
            except RecursionError:
                scope.dynamic = True
                self.diagnostics.append({"path": name, "reason": "ast_depth_limit"})
                self.truncated = True
            parsed_files += 1
        # Attribute writes anywhere in the scanned source invalidate that member.
        for scope, parts in self._mutations:
            target, _ = self._resolve_parts(scope, parts[:-1], set())
            if target is not None:
                if parts[-1] is None:
                    target.dynamic = True
                else:
                    target.mutated_attributes.add(parts[-1])
        for scope, node in self._calls:
            parts = _parts(node.func)
            target, reason = self._resolve_parts(scope, parts, set()) if parts else (None, "dynamic_callable")
            if parts == [scope.self_name]:
                target, reason = None, "dynamic_instance_callable"
            if target is not None and target.symbol["kind"] == "module":
                target, reason = None, "module_is_not_a_callable_declaration"
            try:
                expression = ast.unparse(node.func)[:240]
            except RecursionError:
                expression = "<complex-expression>"
            self.edges.append({"caller": scope.symbol["id"], "callee": target.symbol["id"] if target else None,
                               "path": scope.symbol["path"], "line": node.lineno, "expression": expression,
                               "status": "resolved" if target else "unresolved", "reason": reason})
        self._built = True
        return {"symbols": self.symbols, "edges": self.edges, "diagnostics": self.diagnostics,
                "truncated": self.truncated, "files": parsed_files, "source_bytes": total_bytes,
                "resolved": sum(e["status"] == "resolved" for e in self.edges),
                "unresolved": sum(e["status"] == "unresolved" for e in self.edges),
                "scope": "static_declarations_not_runtime_dispatch"}

    def _lookup(self, scope, name, seen):
        while scope is not None:
            key = (id(scope), name)
            if key in seen or len(seen) > 30:
                return None, "cyclic_or_deep_import"
            seen = seen | {key}
            if scope.star or scope.dynamic:
                return None, "star_import_or_dynamic_scope"
            if name in scope.blocked or name in scope.parameters or name in scope.mutated_attributes:
                return None, "shadowed_or_rebound_name"
            entries = scope.bindings.get(name, [])
            if len(entries) > 1:
                return None, "ambiguous_binding"
            if entries:
                kind, value = entries[0]
                if kind == "import":
                    return self._resolve_dotted(value.split("."), seen)
                if value.decorated:
                    return None, "decorated_declaration"
                return value, "static_declaration"
            scope = scope.parent
            # Class namespaces are not enclosing lexical scopes, including for
            # another nested class. The initial class body can use its own names.
            while scope is not None and scope.symbol["kind"] == "class":
                scope = scope.parent
        return None, "unbound_or_external_name"

    def _member(self, target, parts, seen):
        for part in parts:
            if target.symbol["kind"] not in {"class", "module"}:
                return None, "dynamic_attribute"
            if target.inherited or target.decorated or target.dynamic or target.star:
                return None, "dynamic_or_inherited_member"
            if part in target.blocked or part in target.mutated_attributes:
                return None, "rebound_member"
            entries = target.bindings.get(part, [])
            if len(entries) != 1:
                return None, "unknown_or_ambiguous_member"
            kind, value = entries[0]
            if kind == "import":
                target, reason = self._resolve_dotted(value.split("."), seen)
                if target is None:
                    return None, reason
            else:
                target = value
            if target.decorated:
                return None, "decorated_declaration"
        return target, "static_declaration"

    def _resolve_dotted(self, parts, seen):
        marker = ("import", ".".join(parts))
        if marker in seen or len(seen) > 30:
            return None, "cyclic_or_deep_import"
        seen = seen | {marker}
        # Do not bypass a reassigned package member by jumping to a longer module.
        for end in range(1, len(parts)):
            parents = self._modules.get(".".join(parts[:end]), [])
            if len(parents) > 1:
                return None, "ambiguous_module"
            if parents:
                parent = parents[0]
                if parent.dynamic or parent.star or parts[end] in parent.blocked or parts[end] in parent.mutated_attributes:
                    return None, "rebound_or_dynamic_module_member"
        for end in range(len(parts), 0, -1):
            matches = self._modules.get(".".join(parts[:end]), [])
            if matches:
                if len(matches) != 1:
                    return None, "ambiguous_module"
                return self._member(matches[0], parts[end:], seen)
        return None, "external_or_missing_import"

    def _resolve_parts(self, scope, parts, seen):
        if not parts:
            return None, "dynamic_callable"
        if parts[0] == scope.self_name and scope.owner_class is not None:
            if parts[0] in scope.blocked or scope.dynamic or scope.owner_class.inherited or scope.owner_class.decorated:
                return None, "dynamic_or_inherited_self"
            return self._member(scope.owner_class, parts[1:], seen)
        # An unaliased `import pkg.module` binds `pkg`, even for namespace packages.
        current = scope
        while current is not None:
            if parts[0] in current.blocked or parts[0] in current.parameters or current.star or current.dynamic:
                break
            entries = current.bindings.get(parts[0], [])
            if entries:
                if len(entries) == 1 and entries[0][0] == "import":
                    return self._resolve_dotted(entries[0][1].split(".") + parts[1:], seen)
                break
            current = current.parent
            while current is not None and current.symbol["kind"] == "class":
                current = current.parent
        target, reason = self._lookup(scope, parts[0], seen)
        return self._member(target, parts[1:], seen) if target is not None else (None, reason)

    def _ready(self):
        if not self._built:
            self.build()

    def find_symbols(self, query, limit=20) -> dict:
        if not isinstance(query, str) or not query.strip() or len(query) > 500:
            raise PolicyError("Symbol query must contain 1-500 characters.")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise PolicyError("Symbol limit must be between 1 and 100.")
        self._ready()
        query = query.strip().casefold()
        matches = [s for s in self.symbols if any(query in s[field].casefold() for field in ("name", "qualname", "path", "id"))]
        matches.sort(key=lambda s: (s["name"].casefold() != query, s["qualname"].casefold() != query, s["path"], s["start_line"]))
        return {"symbols": matches[:limit], "truncated": self.truncated or len(matches) > limit}

    def neighbors(self, symbol_id, direction="callees", depth=1, limit=50) -> dict:
        if not isinstance(direction, str) or direction not in {"callers", "callees"} or type(depth) is not int or not 1 <= depth <= 5:
            raise PolicyError("Graph direction must be callers/callees and depth between 1 and 5.")
        if type(limit) is not int or not 1 <= limit <= 200:
            raise PolicyError("Graph result limit must be between 1 and 200.")
        self._ready()
        if not isinstance(symbol_id, str) or symbol_id not in self._scopes:
            raise PolicyError("Unknown symbol ID; find a current symbol before querying neighbors.")
        by_id = {s["id"]: s for s in self.symbols}
        visited = {symbol_id}
        frontier = {symbol_id}
        result_symbols, result_edges, unresolved = [], [], []
        seen_edges = set()
        truncated = self.truncated
        for _ in range(depth):
            next_frontier = set()
            for idx, edge in enumerate(self.edges):
                source = edge["caller"] if direction == "callees" else edge["callee"]
                if source not in frontier or idx in seen_edges:
                    continue
                seen_edges.add(idx)
                if edge["status"] != "resolved":
                    if len(unresolved) < limit:
                        unresolved.append(edge)
                    else:
                        truncated = True
                    continue
                target = edge["callee"] if direction == "callees" else edge["caller"]
                if target not in visited:
                    if len(result_symbols) >= limit:
                        truncated = True
                        continue
                    visited.add(target)
                    next_frontier.add(target)
                    result_symbols.append(by_id[target])
                if len(result_edges) < limit:
                    result_edges.append(edge)
                else:
                    truncated = True
            frontier = next_frontier
            if not frontier:
                break
        return {"symbols": result_symbols, "edges": result_edges, "unresolved": unresolved, "truncated": truncated}
