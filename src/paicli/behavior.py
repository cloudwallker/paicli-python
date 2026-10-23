"""Bounded, auditable Python identifier evidence, not behavior semantics.

Only runtime expression positions and non-placeholder function names contribute.
The source is parsed once in a retrieval snapshot; window filtering is performed
by the retriever so identifiers from omitted source lines cannot leak into hits.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import PurePath


MAX_AST_NODES = 100_000
BEHAVIOR_VOCABULARY = (
    ("checkpoint", ("检查点", "断点"), ("checkpoint", "savepoint")),
    ("expiration", ("过期", "到期", "保留期"), ("expiry", "expiration", "expire", "expired", "expires", "ttl", "retention")),
    ("cleanup", ("清理", "清除"), ("cleanup", "clean", "prune", "purge", "evict", "gc")),
    ("watch", ("监听", "监视", "监控"), ("watch", "watched", "watcher", "watching", "observe", "observed", "observer", "observing", "poll", "polled", "polling", "monitor", "monitored", "monitoring")),
    ("background", ("后台", "守护"), ("background", "daemon")),
)
BEHAVIOR_CONCEPTS = frozenset(name for name, _, _ in BEHAVIOR_VOCABULARY)
BEHAVIOR_POLICY = {
    "version": "python-scope-identifiers-v4",
    "max_ast_nodes": MAX_AST_NODES,
    "required": "all recognized specific behavior concepts in one candidate and one execution scope",
    "evidence": "non-stub function name, runtime Name.Load, loaded attribute, call target, call keyword, daemon attribute assigned literal True; visible complete source lines only",
    "excluded": "comments, strings, imports, class names, parameters, annotations and type aliases, TYPE_CHECKING branches, placeholder functions including excluded-only or statically nonrunning bodies; daemon keyword requires literal True",
    "scope": "module, class body, function, async function, lambda and each comprehension are separate; comprehension first iterable stays in enclosing scope, body/ifs/later iterables in comprehension scope; no cross-scope assembly",
    "selection": "behavior queries gate the complete ranked pool before limiting hits; raw candidates retain top-k ranking; ordinary queries retain the v2 pre-gate limit",
    "conditions": "preserve evaluated Boolean test prefixes even when the result is constant; omit only statically short-circuited operands and nonselected known If branches",
    "failure": "unsupported language, invalid Python, node budget or analysis failure supplies no behavior evidence; ordinary retrieval remains available",
    "meaning": "syntactic lexical evidence only; it does not establish runtime behavior or correctness",
}


@dataclass(frozen=True)
class IdentifierEvidence:
    identifier: str
    line: int
    kind: str
    scope: str


@dataclass(frozen=True)
class BehaviorAnalysis:
    status: str
    node_count: int = 0
    evidence: tuple[IdentifierEvidence, ...] = ()


def _placeholder(statement: ast.stmt) -> bool:
    if isinstance(statement, (ast.Pass, ast.Import, ast.ImportFrom)):
        return True
    if isinstance(statement, ast.If):
        if _test_has_operation(statement.test):
            return False
        truth = _runtime_truth(statement.test)
        if truth is not None:
            return all(_placeholder(child) for child in (statement.body if truth else statement.orelse))
    if isinstance(statement, ast.AnnAssign):
        annotation = statement.annotation
        return (statement.value is None or
                isinstance(annotation, ast.Name) and annotation.id == "TypeAlias" or
                isinstance(annotation, ast.Attribute) and annotation.attr == "TypeAlias")
    if isinstance(statement, getattr(ast, "TypeAlias", ())):
        return True
    if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant):
        return isinstance(statement.value.value, str) or statement.value.value is Ellipsis
    if isinstance(statement, ast.Return):
        value = statement.value
        return (value is None or
                isinstance(value, ast.Constant) and (value.value is None or value.value is Ellipsis) or
                isinstance(value, ast.Name) and value.id == "NotImplemented")
    if isinstance(statement, ast.Raise):
        value = statement.exc
        if isinstance(value, ast.Call):
            value = value.func
        return isinstance(value, ast.Name) and value.id in {"NotImplemented", "NotImplementedError"}
    return False


def _runtime_truth(node: ast.AST) -> bool | None:
    """Recognize obvious non-runtime type-checking and constant branches only."""
    if isinstance(node, ast.Name) and node.id == "TYPE_CHECKING":
        return False
    if isinstance(node, ast.Attribute) and node.attr == "TYPE_CHECKING":
        return False
    if isinstance(node, ast.Constant):
        return bool(node.value)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        inner = _runtime_truth(node.operand)
        return not inner if inner is not None else None
    if isinstance(node, ast.BoolOp):
        values = [_runtime_truth(value) for value in node.values]
        if isinstance(node.op, ast.And):
            if False in values:
                return False
            return True if all(value is True for value in values) else None
        if True in values:
            return True
        return False if all(value is False for value in values) else None
    return None


def _boolean_prefix(node: ast.BoolOp):
    """Yield operands that may run, stopping only at a known short circuit."""
    for value in node.values:
        yield value
        truth = _runtime_truth(value)
        if isinstance(node.op, ast.And) and truth is False:
            break
        if isinstance(node.op, ast.Or) and truth is True:
            break


def _test_has_operation(node: ast.AST) -> bool:
    """An excluded-only test cannot make a placeholder function substantive."""
    if isinstance(node, ast.Constant):
        return False
    if isinstance(node, ast.Name) and node.id == "TYPE_CHECKING":
        return False
    if isinstance(node, ast.Attribute) and node.attr == "TYPE_CHECKING":
        return False
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return _test_has_operation(node.operand)
    if isinstance(node, ast.BoolOp):
        return any(_test_has_operation(value) for value in _boolean_prefix(node))
    return True


class _Identifiers(ast.NodeVisitor):
    def __init__(self):
        self.scope = "<module>"
        self.evidence = set()

    def add(self, identifier: str, line: int, kind: str):
        self.evidence.add(IdentifierEvidence(identifier, line, kind, self.scope))

    def visit_Import(self, node):
        pass

    def visit_ImportFrom(self, node):
        pass

    def visit_Constant(self, node):
        pass

    def visit_arg(self, node):
        pass

    def visit_arguments(self, node):
        pass

    def visit_FunctionDef(self, node):
        previous = self.scope
        self.scope = f"{previous}/function:{node.name}@{node.lineno}"
        if not all(_placeholder(statement) for statement in node.body):
            self.add(node.name, node.lineno, "function_name")
            # Parameters, defaults, decorators and annotations are declarations,
            # deliberately excluded from this conservative lexical evidence.
            for statement in node.body:
                self.visit(statement)
        self.scope = previous

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node):
        previous = self.scope
        self.scope = f"{previous}/class:{node.name}@{node.lineno}"
        # A class name and its bases are not an implementation of that behavior.
        for statement in node.body:
            self.visit(statement)
        self.scope = previous

    def visit_Lambda(self, node):
        previous = self.scope
        self.scope = f"{previous}/lambda@{node.lineno}:{node.col_offset}"
        self.visit(node.body)
        self.scope = previous

    def _visit_comprehension(self, node):
        # Python evaluates the first iterable in the enclosing execution scope.
        # Treat all comprehension kinds alike even when an interpreter version
        # inlines list/set/dict comprehensions as an optimization.
        self.visit(node.generators[0].iter)
        previous = self.scope
        self.scope = f"{previous}/comprehension:{type(node).__name__}@{node.lineno}:{node.col_offset}"
        for offset, generator in enumerate(node.generators):
            if offset:
                self.visit(generator.iter)
            self.visit(generator.target)
            for condition in generator.ifs:
                self.visit(condition)
        if isinstance(node, ast.DictComp):
            self.visit(node.key)
            self.visit(node.value)
        else:
            self.visit(node.elt)
        self.scope = previous

    visit_GeneratorExp = _visit_comprehension
    visit_ListComp = _visit_comprehension
    visit_SetComp = _visit_comprehension
    visit_DictComp = _visit_comprehension

    def visit_AnnAssign(self, node):
        annotation = node.annotation
        if (isinstance(annotation, ast.Name) and annotation.id == "TypeAlias" or
                isinstance(annotation, ast.Attribute) and annotation.attr == "TypeAlias"):
            return
        if node.value is not None:
            self.visit(node.value)

    def visit_TypeAlias(self, node):
        # PEP 695 aliases are declarations with lazily evaluated type values.
        pass

    def visit_Assign(self, node):
        if isinstance(node.value, ast.Constant) and node.value.value is True:
            for target in node.targets:
                if isinstance(target, ast.Attribute) and target.attr == "daemon":
                    self.add(target.attr, target.end_lineno, "enabled_attribute")
        self.generic_visit(node)

    def visit_If(self, node):
        # A constant result does not imply that its test was not evaluated:
        # cleanup() or True still invokes cleanup before selecting the body.
        self.visit(node.test)
        truth = _runtime_truth(node.test)
        if truth is not None:
            for statement in node.body if truth else node.orelse:
                self.visit(statement)
            return
        for statement in node.body + node.orelse:
            self.visit(statement)

    def visit_BoolOp(self, node):
        for value in _boolean_prefix(node):
            self.visit(value)

    def visit_Name(self, node):
        if isinstance(node.ctx, ast.Load):
            self.add(node.id, node.lineno, "name_load")

    def visit_Attribute(self, node):
        if isinstance(node.ctx, ast.Load):
            # An attribute may follow its receiver on a continuation line. Its
            # end line locates the attribute itself, unlike node.lineno.
            self.add(node.attr, node.end_lineno, "attribute_load")
        self.visit(node.value)

    def visit_Call(self, node):
        if isinstance(node.func, ast.Name):
            self.add(node.func.id, node.func.lineno, "call")
        elif isinstance(node.func, ast.Attribute):
            self.add(node.func.attr, node.func.end_lineno, "call")
        self.generic_visit(node)

    def visit_keyword(self, node):
        if node.arg is not None:
            # daemon=False, None, 0 or an unevaluated variable is no positive
            # evidence of background work. Literal True is an auditable signal.
            if node.arg != "daemon" or isinstance(node.value, ast.Constant) and node.value.value is True:
                self.add(node.arg, node.lineno, "keyword")
        self.visit(node.value)


def analyze_source(path: str, source: str) -> BehaviorAnalysis:
    """Fail closed for the behavior layer without disabling ordinary retrieval."""
    if PurePath(path).suffix.lower() != ".py":
        return BehaviorAnalysis("unsupported_language")
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return BehaviorAnalysis("syntax_error")
    except (RecursionError, MemoryError, OverflowError):
        return BehaviorAnalysis("analysis_error")
    count = 0
    pending = [tree]
    while pending:
        node = pending.pop()
        count += 1
        if count > MAX_AST_NODES:
            return BehaviorAnalysis("node_budget_exceeded", count)
        pending.extend(ast.iter_child_nodes(node))
    try:
        identifiers = _Identifiers()
        identifiers.visit(tree)
    except (RecursionError, MemoryError, OverflowError):
        return BehaviorAnalysis("analysis_error", count)
    evidence = tuple(sorted(identifiers.evidence, key=lambda item: (item.line, item.scope, item.identifier, item.kind)))
    return BehaviorAnalysis("ok", count, evidence)
