"""Bounded bilingual lexical evidence, with an explicit heuristic abstention gate.

This is neither semantic retrieval nor a probability of implementation. The fixed
vocabulary contains general computing concepts, never repository paths, expected
answers, evaluation labels, or lists of products presumed absent.
"""
from __future__ import annotations

from collections import Counter
import copy
from dataclasses import dataclass
import hashlib
import json
import math
import re
from types import MappingProxyType
import unicodedata

from .errors import PolicyError
from .index import CodeIndex, MAX_FILE_BYTES
from .behavior import (BEHAVIOR_CONCEPTS, BEHAVIOR_POLICY, BEHAVIOR_VOCABULARY,
                       BehaviorAnalysis, analyze_source)


MAX_CHUNKS = 10_000
MAX_CONTENT_BYTES = 8 * 1024 * 1024
MAX_VISIBLE_CHARS = 8_000
COVERAGE_THRESHOLD = 0.6
ALGORITHM_VERSION = "bilingual-concept-bm25-v3"
SHORT_QUERY_MAX_CONCEPTS = 3

# This syntactic policy is fixed before held-out evaluation. It does not infer
# whether a requested behavior exists. Unknown prose remains visible in ranking
# and diagnostics; only explicit anchors and short keyword queries are strict.
_ANCHOR_RULES = {
    "short_query": "at most 3 distinct non-stopword concepts; every unknown literal is required",
    "explicit": "backtick identifiers; underscore or mixed-case identifiers; uppercase acronyms; unknown titlecase words except the first run; non-Han letter runs in mixed-Han queries",
    "matching": "NFC+casefold exact identifier or contiguous components within one identifier; Han literal substring; no cross-identifier assembly or inflection",
    "natural_language": "other unknown words are soft ranking evidence; coverage denominator is known concepts plus anchors",
    "empty_gate": "no known concept and no anchor means insufficient evidence",
    "stopword_precedence": "explicit anchors are recognized before stopword filtering",
    "anchor_only_ranking": "anchors may create candidates; if no concept matches, sum binary-frequency BM25 per matched anchor",
}

# Each row describes one concept. Alternative spellings are alternatives, not
# extra votes. These general-purpose terms are independent of any indexed code.
_VOCABULARY = (
    ("context", ("上下文",), ("context", "ctx")),
    ("dependency", ("依赖", "依赖项"), ("dependency", "dependencies", "depend", "requires", "require")),
    ("loop", ("循环", "迭代"), ("loop", "iteration", "iterate", "while", "for")),
    ("compression", ("压缩", "精简"), ("compact", "compaction", "compress", "compression")),
    ("history", ("历史", "历史记录"), ("history", "historical")),
    ("file", ("文件",), ("file", "filename")),
    ("path", ("路径", "目录"), ("path", "directory", "directories", "folder")),
    ("permission", ("权限", "授权"), ("permission", "authorization", "authorize", "access")),
    ("approval", ("审批", "批准", "许可"), ("approval", "approve", "consent")),
    ("timeout", ("超时",), ("timeout", "deadline")),
    ("session", ("会话",), ("session",)),
    ("concurrency", ("并发", "并行"), ("concurrency", "concurrent", "parallel", "parallelism")),
    ("idempotency", ("幂等", "幂等性"), ("idempotency", "idempotent")),
    ("index", ("索引",), ("index", "indexes", "indices", "indexing")),
    ("cache", ("缓存",), ("cache", "caching", "cached")),
    ("network", ("网络", "联网"), ("network", "networking", "socket")),
    ("retry", ("重试",), ("retry", "retries", "retrying")),
    ("backoff", ("退避",), ("backoff",)),
    ("error", ("错误", "失败", "报错"), ("error", "failure", "failed", "fail")),
    ("exception", ("异常",), ("exception", "raise", "except", "throw", "catch")),
    ("validation", ("校验", "验证", "检查"), ("validation", "validate", "verify", "check")),
    ("security", ("安全",), ("security", "secure", "safe", "safety")),
    ("secret", ("密钥", "秘密", "凭据", "密码"), ("secret", "credential", "password")),
    ("configuration", ("配置",), ("configuration", "config", "settings", "setting")),
    ("environment", ("环境", "环境变量"), ("environment", "environ", "env")),
    ("serialization", ("序列化",), ("serialization", "serialize", "dump", "dumps")),
    ("stream", ("流式", "流处理", "数据流"), ("stream", "streaming")),
    ("log", ("日志", "记录日志"), ("log", "logging", "logger")),
    ("command", ("命令", "命令行"), ("command", "cmd", "cli")),
    ("argument", ("参数",), ("argument", "arg", "parameter", "param")),
    ("process", ("进程", "子进程"), ("process", "subprocess")),
    ("thread", ("线程",), ("thread", "threading")),
    ("queue", ("队列",), ("queue", "enqueue", "dequeue")),
    ("async", ("异步",), ("async", "asynchronous", "await", "asyncio")),
    ("sync", ("同步",), ("sync", "synchronous")),
    ("request", ("请求",), ("request",)),
    ("response", ("响应", "返回值"), ("response",)),
    ("transaction", ("事务",), ("transaction", "atomic", "atomicity")),
    ("rollback", ("回滚", "撤销"), ("rollback", "undo", "revert")),
    ("connection", ("连接",), ("connection", "connect")),
    ("database", ("数据库",), ("database", "db")),
    ("search", ("搜索",), ("search",)),
    ("retrieval", ("检索", "召回"), ("retrieval", "retrieve", "retriever")),
    ("ranking", ("排序", "重排"), ("rank", "ranking", "rerank", "sort", "sorted")),
    ("chunk", ("分块", "片段", "切块"), ("chunk", "chunking", "segment")),
    ("embedding", ("嵌入", "向量化"), ("embedding", "embed")),
    ("vector", ("向量",), ("vector",)),
    ("graph", ("图谱", "依赖图", "调用图"), ("graph",)),
    ("node", ("节点",), ("node", "vertex")),
    ("edge", ("边",), ("edge",)),
    ("reference", ("引用",), ("reference", "ref")),
    ("call", ("调用",), ("call", "invoke", "invocation")),
    ("parse", ("解析",), ("parse", "parser", "parsing")),
    ("syntax", ("语法", "抽象语法树"), ("syntax", "ast")),
    ("storage", ("存储", "持久化"), ("storage", "store", "persist", "persistence")),
    ("summary", ("摘要", "总结"), ("summary", "summarize", "summarization")),
    ("memory", ("内存", "记忆"), ("memory",)),
    ("read", ("读取", "读文件"), ("read", "reader")),
    ("write", ("写入", "写文件"), ("write", "writer")),
    ("delete", ("删除",), ("delete", "remove", "unlink")),
    ("create", ("创建", "新建"), ("create", "new")),
    ("update", ("更新", "修改"), ("update", "modify")),
    ("limit", ("限制", "上限", "限额"), ("limit", "maximum", "max", "quota", "bound", "bounded")),
    ("budget", ("预算",), ("budget",)),
    ("cancel", ("取消", "中止"), ("cancel", "cancellation", "abort")),
    ("tool", ("工具",), ("tool",)),
    ("message", ("消息",), ("message",)),
    ("token", ("令牌", "词元"), ("token",)),
    ("lock", ("锁", "加锁", "互斥"), ("lock", "mutex", "locking")),
    ("conflict", ("冲突",), ("conflict",)),
    ("format", ("格式",), ("format",)),
    ("test", ("测试",), ("test", "testing")),
    ("event", ("事件",), ("event",)),
    ("task", ("任务",), ("task", "job")),
    ("schedule", ("调度",), ("schedule", "scheduler", "scheduling")),
    ("resource", ("资源",), ("resource",)),
    ("model", ("模型",), ("model",)),
    ("prompt", ("提示词",), ("prompt",)),
    ("output", ("输出",), ("output", "stdout")),
    ("input", ("输入",), ("input", "stdin")),
    ("authentication", ("认证", "身份验证"), ("authentication", "authenticate", "auth")),
    ("sandbox", ("沙箱", "隔离"), ("sandbox", "isolation", "isolate")),
) + BEHAVIOR_VOCABULARY

_EN_STOP = frozenset("a an the this that these those is are was were be been being do does did how what why where when which who can could would should will shall may might must to of for in on at by with from and or but if then as it its they them their we our you your i me my please explain describe show tell code project function method implementation implement implemented feature functionality use uses using work works working support supports supported handle handles handling get make have has had not no into about".split())
_ZH_STOP = frozenset("这个 这些 这里 那个 这些 项目 代码 函数 方法 功能 实现 使用 如何 怎样 怎么 为什么 什么 哪些 哪里 哪个 是否 有没有 能不能 支持 机制 流程 设计 处理 进行 以及 通过 其中 里面 一个 一下 相关 具体 可以 需要 请问 解释 介绍 说明 讲讲 作用 区别 之间 还有 例如 它们 你们 我们 应当 应该 采用 能够 关于 不会 避免 防止 保证 保障 确保 解决 工作 运作 请 是 有 的 了 吗 呢 和 与 在 中 为 对 把 将 等 会 能 从 来 做 得 着 到 后 前 时 及 或 不 它 你 我".split())

# Deliberately limited inflection rules: unknown product names are not stemmed.
_IRREGULAR = {"dependencies": "dependency", "directories": "directory", "indices": "index", "indexes": "index", "retries": "retry", "cached": "cache", "caching": "cache", "compressed": "compress", "compressing": "compress", "compacted": "compact", "compacting": "compact", "approved": "approve", "approving": "approve", "validated": "validate", "validating": "validate", "verified": "verify", "written": "write", "writing": "write", "reading": "read", "deleted": "delete", "deleting": "delete", "created": "create", "creating": "create", "updated": "update", "updating": "update", "connected": "connect", "connecting": "connect"}
_PLURAL_BASES = frozenset({alias for _, _, aliases in _VOCABULARY for alias in aliases} | {"value", "second", "byte", "character", "line", "word", "step", "result", "record", "item", "attempt", "client", "server"})
_PARTS = re.compile(r"[A-Z]+(?=[A-Z][a-z]|[0-9]|$)|[A-Z]?[a-z]+|[0-9]+")
_CJK = re.compile(r"^[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\U00020000-\U0002ffff\U00030000-\U000323af]+$")
_LINES = re.compile(r"[^\r\n]*(?:\r\n|\r|\n|$)")


def _normalize(word: str) -> str:
    word = unicodedata.normalize("NFC", word.casefold())
    if word in _IRREGULAR:
        return _IRREGULAR[word]
    if word.endswith("s") and word[:-1] in _PLURAL_BASES:
        return word[:-1]
    return word


_CONCEPT_ALIASES = {name: tuple(sorted(set(zh) | {_normalize(a) for a in en})) for name, zh, en in _VOCABULARY}
_ALIAS_CONCEPT = {alias: name for name, aliases in _CONCEPT_ALIASES.items() for alias in aliases}
_ZH_WORDS = tuple(sorted({term for _, zh, _ in _VOCABULARY for term in zh} | _ZH_STOP, key=lambda term: (-len(term), term)))


def _chinese_parts(text: str):
    offset = 0
    unknown = []
    while offset < len(text):
        match = next((word for word in _ZH_WORDS if text.startswith(word, offset)), None)
        if match is None:
            unknown.append(text[offset])
            offset += 1
            continue
        if unknown:
            yield "".join(unknown)
            unknown.clear()
        yield match
        offset += len(match)
    if unknown:
        yield "".join(unknown)


def _identifier_runs(text: str):
    """Keep all Unicode letters, numbers and marks instead of ASCII truncation."""
    current = []
    current_kind = None
    for character in unicodedata.normalize("NFC", text):
        if _CJK.fullmatch(character):
            kind = "han"
        elif character == "_" or unicodedata.category(character)[0] in {"L", "N", "M"}:
            kind = "word"
        else:
            kind = None
        if kind != current_kind or kind is None:
            if current:
                yield "".join(current)
                current.clear()
        if kind is not None:
            current.append(character)
        current_kind = kind
    if current:
        yield "".join(current)


def _parts(text: str):
    for identifier in _identifier_runs(text):
        if _CJK.fullmatch(identifier):
            yield from _chinese_parts(identifier)
        else:
            for snake_part in identifier.split("_"):
                if snake_part.isascii():
                    yield from (_normalize(part) for part in _PARTS.findall(snake_part))
                elif snake_part:
                    yield _normalize(snake_part)


def _identifier_components(identifier: str) -> tuple[str, ...]:
    """Case folding without stemming: a named entity is not a synonym."""
    parts = []
    for segment in identifier.split("_"):
        if segment.isascii():
            parts.extend(_PARTS.findall(segment))
        elif segment:
            parts.append(segment)
    return tuple(unicodedata.normalize("NFC", part.casefold()) for part in parts)


def _hash(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


_VOCABULARY_HASH = _hash({"concepts": _VOCABULARY, "inflections": _IRREGULAR, "plurals": sorted(_PLURAL_BASES)})
_PROFILE = {
    "algorithm_version": ALGORITHM_VERSION,
    "vocabulary_hash": _VOCABULARY_HASH,
    "stopwords_hash": _hash({"english": sorted(_EN_STOP), "chinese": sorted(_ZH_STOP)}),
    "unicode_normalization": "NFC+casefold; preserve letters,numbers,marks; split Han runs",
    "unicode_version": unicodedata.unidata_version,
    "threshold": COVERAGE_THRESHOLD,
    "ranking": "coverage-desc,bm25-max-per-concept-desc,path,start_line,end_line,symbol",
    "gate": "known-concept-and-anchor-coverage-at-least-threshold-and-all-anchors-in-visible-window-and-all-required-behaviors-in-one-visible-execution-scope",
    "anchor_rules": _ANCHOR_RULES,
    "behavior_policy": BEHAVIOR_POLICY,
    "behavior_vocabulary_hash": _hash(BEHAVIOR_VOCABULARY),
    "short_query_max_concepts": SHORT_QUERY_MAX_CONCEPTS,
    "bm25_k1": 1.2,
    "bm25_b": 0.75,
    "max_chunks": MAX_CHUNKS,
    "max_content_bytes": MAX_CONTENT_BYTES,
    "max_source_bytes": MAX_FILE_BYTES,
    "max_visible_chars": MAX_VISIBLE_CHARS,
    "line_boundaries": "CRLF,CR,LF; oversized single lines omitted and counted",
}
_PROFILE["profile_hash"] = _hash(_PROFILE)


@dataclass(frozen=True)
class _Window:
    path: str
    symbol: str
    start_line: int
    end_line: int
    content: str
    search_text: str
    terms: object
    length: int
    identifiers: tuple
    behavior_analysis: BehaviorAnalysis
    behavior_evidence: tuple


def _read_source(workspace, name: str):
    """Only Workspace-approved regular files, with a bounded read even after stat."""
    try:
        target = workspace.resolve(name)
        if not target.is_file():
            return None
        if target.stat().st_size > MAX_FILE_BYTES:
            raise PolicyError("Retrieval source exceeds the 512 KiB file budget.")
        with target.open("rb") as stream:
            raw = stream.read(MAX_FILE_BYTES + 1)
    except OSError:
        return None
    if len(raw) > MAX_FILE_BYTES:
        raise PolicyError("Retrieval source exceeds the 512 KiB file budget.")
    return raw


def _query_concepts(query: str) -> tuple[list[dict], list[str]]:
    concepts = {}
    ignored = []
    for part in _parts(query):
        if part in _ZH_STOP or part in _EN_STOP:
            if part not in ignored:
                ignored.append(part)
            continue
        canonical = _ALIAS_CONCEPT.get(part)
        key = canonical if canonical is not None else "literal:" + part
        if key not in concepts:
            concepts[key] = {"term": canonical or part, "kind": "concept" if canonical else "literal", "source_terms": [], "alternatives": list(_CONCEPT_ALIASES[canonical]) if canonical else [part]}
        if part not in concepts[key]["source_terms"]:
            concepts[key]["source_terms"].append(part)
    return list(concepts.values()), ignored


def _query_policy(query: str, concepts: list[dict]) -> dict:
    anchors = {}

    def add(identifier, reason):
        term = unicodedata.normalize("NFC", identifier.casefold())
        if term not in anchors:
            anchors[term] = {"term": term, "source_term": identifier, "parts": list(_identifier_components(identifier)), "reasons": []}
        if reason not in anchors[term]["reasons"]:
            anchors[term]["reasons"].append(reason)

    for quoted in re.findall(r"`([^`]+)`", query):
        for identifier in _identifier_runs(quoted):
            add(identifier, "backtick_identifier")
    runs = list(_identifier_runs(query))
    mixed_han = any(_CJK.fullmatch(run) for run in runs)
    for position, identifier in enumerate(runs):
        if _CJK.fullmatch(identifier) or not any(character.isalpha() for character in identifier):
            continue
        normalized = unicodedata.normalize("NFC", identifier.casefold())
        components = _identifier_components(identifier)
        if "_" in identifier or (len(components) > 1 and any(character.isupper() for character in identifier)):
            add(identifier, "structured_identifier")
        if len(identifier) > 1 and identifier.isupper():
            add(identifier, "uppercase_acronym")
        if mixed_han:
            add(identifier, "mixed_language_identifier")
        if normalized in _EN_STOP:
            continue
        if position > 0 and identifier.istitle() and normalized not in _ALIAS_CONCEPT:
            add(identifier, "titlecase_name")
    short = len(concepts) <= SHORT_QUERY_MAX_CONCEPTS
    anchored_parts = {_normalize(part) for anchor in anchors.values() for part in anchor["parts"]}
    if short:
        for concept in concepts:
            if concept["kind"] == "literal" and concept["term"] not in anchored_parts:
                add(concept["term"], "short_query_literal")
    anchored_parts = {_normalize(part) for anchor in anchors.values() for part in anchor["parts"]}
    return {
        "mode": "keywords" if short else "natural_language",
        "required_anchors": list(anchors.values()),
        "gate_concepts": [concept["term"] for concept in concepts if concept["kind"] == "concept"],
        "required_behaviors": [concept["term"] for concept in concepts if concept["term"] in BEHAVIOR_CONCEPTS],
        "soft_terms": [concept["term"] for concept in concepts if concept["kind"] == "literal" and concept["term"] not in anchored_parts],
    }


def _anchor_matches(window: _Window, anchor: dict) -> bool:
    term = anchor["term"]
    if _CJK.fullmatch(term):
        return term in window.search_text
    wanted = tuple(anchor["parts"])
    if not wanted:
        return False
    for identifier, components in window.identifiers:
        if identifier == term:
            return True
        if any(components[offset:offset + len(wanted)] == wanted for offset in range(len(components) - len(wanted) + 1)):
            return True
    return False


def _frequency(window: _Window, term: str) -> int:
    # Literal Chinese names remain searchable inside longer comments; there is
    # no fallback from an unrecognized Chinese question to all code chunks.
    return window.search_text.count(term) if _CJK.fullmatch(term) else window.terms.get(term, 0)


def _behavior_matches(window: _Window, required: list[str]) -> dict:
    analysis = {"status": window.behavior_analysis.status,
                "node_count": window.behavior_analysis.node_count}
    if not required:
        return {"matched_behaviors": [], "missing_behaviors": [], "behavior_evidence": [],
                "behavior_scope_satisfied": True, "behavior_supporting_scopes": [],
                "behavior_analysis": analysis}
    by_scope = {}
    records = []
    for evidence in window.behavior_evidence:
        concepts = {_ALIAS_CONCEPT.get(part) for part in _parts(evidence.identifier)}
        for behavior in required:
            if behavior in concepts:
                by_scope.setdefault(evidence.scope, set()).add(behavior)
                records.append({"behavior": behavior, "identifier": evidence.identifier,
                                "line": evidence.line, "kind": evidence.kind, "scope": evidence.scope})
    matched = {behavior for found in by_scope.values() for behavior in found}
    supporting = sorted(scope for scope, found in by_scope.items() if set(required) <= found)
    return {"matched_behaviors": [behavior for behavior in required if behavior in matched],
            "missing_behaviors": [behavior for behavior in required if behavior not in matched],
            "behavior_evidence": records, "behavior_scope_satisfied": not required or bool(supporting),
            "behavior_supporting_scopes": supporting,
            "behavior_analysis": analysis}


class CodeRetriever:
    """Reusable read-only index snapshot. Rebuild after changing the index.

    Every search revalidates source paths and digests, so a snapshot never serves
    changed/deleted/protected source. Workspace's application-level path policy
    does not replace OS isolation against hostile concurrent filesystem races.
    """

    def __init__(self, index: CodeIndex):
        self._workspace = index.workspace
        count, content_bytes, bad_metadata = index.store.connection.execute("""
            SELECT COUNT(*), COALESCE(SUM(length(CAST(content AS BLOB))), 0),
                   COALESCE(MAX(length(path) > 4096 OR length(symbol) > 1024
                                OR length(digest) > 64 OR typeof(content) != 'text'
                                OR typeof(path) != 'text' OR typeof(symbol) != 'text'
                                OR typeof(digest) != 'text'), 0)
            FROM chunks
        """).fetchone()
        if count > MAX_CHUNKS or content_bytes > MAX_CONTENT_BYTES:
            raise PolicyError("Retrieval snapshot exceeds the chunk or aggregate content budget.")
        if bad_metadata:
            raise PolicyError("Retrieval index contains invalid metadata.")
        windows = []
        source_cache = {}
        stamps = {}
        seen_windows = set()
        source_bytes = 0
        read_chunks = 0
        read_content_bytes = 0
        excluded_chunks = 0
        omitted_lines = 0
        rows = index.store.connection.execute("SELECT path,symbol,start_line,end_line,content,digest FROM chunks ORDER BY path,start_line,end_line,symbol,id")
        for path, symbol, start, end, content, digest in rows:
            if not isinstance(content, str):
                raise PolicyError("Retrieval index contains invalid content.")
            read_chunks += 1
            read_content_bytes += len(content.encode("utf-8"))
            if read_chunks > MAX_CHUNKS or read_content_bytes > MAX_CONTENT_BYTES:
                raise PolicyError("Retrieval snapshot exceeds the chunk or aggregate content budget.")
            if not isinstance(path, str) or len(path) > 4096 or not isinstance(symbol, str) or len(symbol) > 1024:
                raise PolicyError("Retrieval index contains invalid metadata.")
            if path not in source_cache:
                # Policy denials are exclusions; byte-budget denials must abort.
                try:
                    self._workspace.resolve(path)
                except (OSError, PolicyError):
                    source_cache[path] = None
                else:
                    raw = _read_source(self._workspace, path)
                    if raw is None:
                        source_cache[path] = None
                    else:
                        source_bytes += len(raw)
                        if source_bytes > MAX_CONTENT_BYTES:
                            raise PolicyError("Retrieval sources exceed the 8 MiB aggregate source budget.")
                        try:
                            text = raw.decode("utf-8")
                        except UnicodeError:
                            source_cache[path] = None
                        else:
                            source_cache[path] = (hashlib.sha256(raw).hexdigest(), tuple(line for line in _LINES.findall(text) if line), analyze_source(path, text))
            source = source_cache[path]
            if source is None or digest != source[0] or type(start) is not int or type(end) is not int or not 1 <= start <= end <= len(source[1]):
                excluded_chunks += 1
                continue
            lines = source[1][start - 1:end]
            if content != "".join(lines):
                excluded_chunks += 1
                continue
            stamps[path] = digest
            current = []
            current_chars = 0
            window_start = start

            def add_window():
                if not current:
                    return
                visible = "".join(current)
                window_end = window_start + len(current) - 1
                key = (path, symbol, window_start, window_end)
                if key in seen_windows:
                    return
                seen_windows.add(key)
                terms = Counter(_parts(visible))
                search_text = unicodedata.normalize("NFC", visible.casefold())
                identifiers = tuple(sorted({(unicodedata.normalize("NFC", identifier.casefold()), _identifier_components(identifier)) for identifier in _identifier_runs(visible)}))
                behavior_evidence = tuple(item for item in source[2].evidence if window_start <= item.line <= window_end)
                windows.append(_Window(path, symbol, window_start, window_end, visible, search_text, MappingProxyType(dict(terms)), sum(terms.values()), identifiers, source[2], behavior_evidence))

            for line_number, line in enumerate(lines, start):
                if len(line) > MAX_VISIBLE_CHARS:
                    add_window()
                    current = []
                    current_chars = 0
                    omitted_lines += 1
                    window_start = line_number + 1
                    continue
                if current and current_chars + len(line) > MAX_VISIBLE_CHARS:
                    add_window()
                    current = []
                    current_chars = 0
                if not current:
                    window_start = line_number
                current.append(line)
                current_chars += len(line)
            add_window()
        self._windows = tuple(windows)
        self._stamps = MappingProxyType(stamps)
        snapshot_identity = {"files": sorted(stamps.items()), "windows": [(window.path, window.symbol, window.start_line, window.end_line) for window in windows]}
        behavior_statuses = Counter(source[2].status for source in source_cache.values() if source is not None)
        self._profile = {**_PROFILE, "snapshot_chunks": read_chunks, "snapshot_content_bytes": read_content_bytes, "snapshot_source_bytes": source_bytes, "snapshot_windows": len(windows), "excluded_chunks": excluded_chunks, "omitted_oversize_lines": omitted_lines, "behavior_analysis_statuses": dict(behavior_statuses), "snapshot_hash": _hash(snapshot_identity)}

    def search(self, query: str, limit: int = 5) -> dict:
        if not isinstance(query, str) or not query.strip() or len(query) > 500:
            raise PolicyError("Search query must contain 1–500 characters.")
        if type(limit) is not int or not 1 <= limit <= 20:
            raise PolicyError("Search limit must be between 1 and 20.")
        concepts, ignored = _query_concepts(query)
        policy = _query_policy(query, concepts)
        anchors = policy["required_anchors"]
        required_behaviors = policy["required_behaviors"]
        gate_concepts = set(policy["gate_concepts"])
        evidence_count = len(gate_concepts) + len(anchors)
        valid_paths = set()
        source_bytes = 0
        for path, expected_digest in self._stamps.items():
            try:
                self._workspace.resolve(path)
            except (OSError, PolicyError):
                continue
            raw = _read_source(self._workspace, path)
            if raw is None:
                continue
            source_bytes += len(raw)
            if source_bytes > MAX_CONTENT_BYTES:
                raise PolicyError("Retrieval sources exceed the 8 MiB aggregate source budget.")
            if hashlib.sha256(raw).hexdigest() == expected_digest:
                valid_paths.add(path)
        available = [window for window in self._windows if window.path in valid_paths]
        alternatives = {term for concept in concepts for term in concept["alternatives"]}
        frequencies = [{term: count for term in alternatives if (count := _frequency(window, term))} for window in available]
        document_frequency = Counter(term for terms in frequencies for term in terms)
        anchor_matches = [[anchor["term"] for anchor in anchors if _anchor_matches(window, anchor)] for window in available]
        anchor_document_frequency = Counter(term for matches in anchor_matches for term in matches)
        avg_length = sum(window.length for window in available) / max(len(available), 1) or 1
        supported = {concept["term"] for concept in concepts if any(term in document_frequency for term in concept["alternatives"])}
        unsupported = [concept["term"] for concept in concepts if concept["term"] not in supported]
        candidates = []
        for window, terms, matched_anchors in zip(available, frequencies, anchor_matches):
            matched = []
            matches = []
            score = 0.0
            for concept in concepts:
                found = [term for term in concept["alternatives"] if term in terms]
                if not found:
                    continue
                matched.append(concept["term"])
                matches.append({"term": concept["term"], "matched_alternatives": found})
                scores = []
                for term in found:
                    tf = terms[term]
                    df = document_frequency[term]
                    idf = math.log(1 + (len(available) - df + 0.5) / (df + 0.5))
                    scores.append(idf * tf * 2.2 / (tf + 1.2 * (0.25 + 0.75 * window.length / avg_length)))
                score += max(scores)
            if not matched and not matched_anchors:
                continue
            score_kind = "BM25 maximum per concept; not a probability"
            if not matched:
                for term in matched_anchors:
                    df = anchor_document_frequency[term]
                    idf = math.log(1 + (len(available) - df + 0.5) / (df + 0.5))
                    score += idf * 2.2 / (1 + 1.2 * (0.25 + 0.75 * window.length / avg_length))
                score_kind = "Binary-frequency BM25 per explicit anchor; not a probability"
            coverage = len(matched) / len(concepts) if concepts else len(matched_anchors) / len(anchors)
            missing = [concept["term"] for concept in concepts if concept["term"] not in matched]
            missing_anchors = [anchor["term"] for anchor in anchors if anchor["term"] not in matched_anchors]
            evidence_coverage = (len(gate_concepts.intersection(matched)) + len(matched_anchors)) / evidence_count if evidence_count else 0.0
            candidates.append({"path": window.path, "symbol": window.symbol, "start_line": window.start_line, "end_line": window.end_line, "content": window.content, "score": score, "lexical_coverage": coverage, "evidence_coverage": evidence_coverage, "matched_anchors": matched_anchors, "missing_anchors": missing_anchors, "matched_terms": matched, "unsupported_terms": missing, "matches": matches, "score_kind": score_kind, **_behavior_matches(window, required_behaviors)})
        candidates.sort(key=lambda hit: (-hit["lexical_coverage"], -hit["score"], hit["path"], hit["start_line"], hit["end_line"], hit["symbol"]))
        # Behavior evidence can reject an arbitrary number of otherwise strong
        # lexical matches, such as comments. Check the ranked pool before its
        # hit limit so those decoys cannot hide a valid implementation. Keep
        # raw top-k candidates for offline ranking comparisons and preserve the
        # original limit policy for queries without a specific behavior.
        checked = candidates if required_behaviors else candidates[:limit]
        hits = [dict(candidate) for candidate in checked if candidate["evidence_coverage"] >= COVERAGE_THRESHOLD and not candidate["missing_anchors"] and candidate["behavior_scope_satisfied"]][:limit]
        best_coverage = max((candidate["evidence_coverage"] for candidate in checked), default=0.0)
        missing_anchors = [anchor["term"] for anchor in anchors if not any(anchor["term"] in candidate["matched_anchors"] for candidate in checked)]
        missing_behaviors = [behavior for behavior in required_behaviors if not any(behavior in candidate["matched_behaviors"] for candidate in checked)]
        if not concepts and not anchors:
            reason = "no_searchable_terms"
        elif missing_anchors:
            reason = "unsupported_required_anchors"
        elif not checked:
            reason = "no_visible_lexical_evidence"
        elif not evidence_count:
            reason = "no_gate_evidence_terms"
        elif not hits and missing_behaviors:
            reason = "unsupported_required_behaviors"
        elif not hits and required_behaviors and not any(candidate["behavior_scope_satisfied"] for candidate in checked):
            reason = ("required_behaviors_not_in_same_scope" if any(not candidate["missing_behaviors"] for candidate in checked)
                      else "required_behaviors_not_in_same_candidate")
        elif best_coverage < COVERAGE_THRESHOLD:
            reason = "coverage_below_threshold"
        elif not hits:
            reason = "required_anchors_not_in_same_candidate"
        else:
            reason = "sufficient_lexical_coverage"
        return {
            "hits": hits,
            "candidates": candidates[:limit],
            "decision": {"status": "accepted" if hits else "insufficient_evidence", "reason": reason, "threshold": COVERAGE_THRESHOLD, "best_coverage": best_coverage, "unsupported_terms": unsupported, "missing_anchors": missing_anchors, "missing_behaviors": missing_behaviors, "evidence_term_count": evidence_count, "meaning": "Heuristic lexical evidence only; insufficient evidence does not prove absence of an implementation. Unknown prose is soft evidence. Specific behavior concepts require visible Python identifiers in one execution scope, which still does not establish runtime semantics or correctness."},
            "query": {"original": query, "concepts": concepts, "ignored_terms": ignored, **policy, "expansion": "General computing synonyms; snake_case/CamelCase/acronym splitting; NFC+casefold; Unicode literals retained; limited inflections; one vote per concept. Explicit anchors match within one identifier; ordinary prose is soft evidence."},
            "profile": {**copy.deepcopy(self._profile), "unavailable_files": len(self._stamps) - len(valid_paths)},
        }
