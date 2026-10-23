# PaiCLI Python

**A Python coding agent and AI platform operations CLI with observable tool calls, exact write approvals, and repeatable offline examples.**

English | [简体中文](README_ZH.md)

PaiCLI Python combines a bounded model/tool loop, local code retrieval, session persistence, and read-only platform diagnostics. It is a focused Python implementation for learning, demonstration, and further engineering work.

![Formatted real offline demo output showing code indexing, agent tools, denied writes, and a completed DAG](docs/images/demo.png)

*Formatted real offline demo output, not a terminal screenshot. The model replies are explicitly scripted; file tools, SQLite storage, retrieval, and scheduling execute real code.*

## Quick start

Requires **Python 3.11+** with SQLite FTS5 support. Run from this repository:

```sh
python -m venv .venv
# Linux/macOS: source .venv/bin/activate
# Windows PowerShell: .\.venv\Scripts\Activate.ps1
python -m pip install .
paicli demo --json
```

`demo` needs no API key or network access. It creates and cleans up a temporary workspace, indexes a Python fixture, runs two Agent tools, verifies that an unapproved write is rejected, and executes a three-node DAG. The fixture demonstrates process-local refund idempotency; it is not production payment code.

## What is implemented

| Capability | Implementation |
| --- | --- |
| Agent | ReAct-style request/tool/result loop; non-streaming OpenAI-compatible chat completions and an explicit scripted provider |
| Tools | `read_file`, `list_files`, `search_code`, `retrieve_code`, `find_symbol`, `callers`, `callees`, `write_file`; declared argument schemas and source-path checks |
| Write approval | Full diff and a SHA-256 fingerprint bound to the relative path, previous content, and proposed content; exact interactive approval for every write |
| Sessions | SQLite ledger of system, user, assistant, and tool messages; resumable session IDs and deterministic excerpts of older turns |
| Code retrieval | Python AST chunks plus imports/constants/top-level code, JSX/MJS/CSS/HTML support, bounded windows, FTS5/BM25 ranking, source locations, and stale-content rejection |
| Bilingual retrieval | Experimental Chinese/English concept matching, identifier splitting, deterministic lexical ranking, scope-bound Python identifier evidence, and explicit insufficient-evidence results |
| Plans | Validated dependency DAG, bounded `asyncio` concurrency, dependency-result handoff, failure cancellation, and read-only Agent workers |
| Platform operations | DNS/TCP/HTTP diagnostics, authenticated FlowTrail identity/quota/runs/details/event replay, and a read-only POSIX world-writable-file audit |

## Run the included examples

```sh
paicli index --workspace examples/codebase
paicli search "refund_order request_key" --workspace examples/codebase
paicli agent "Inspect refund idempotency" --workspace examples/codebase --script examples/read-and-search.json --session refund-review
paicli plan examples/plan.json --workspace examples/codebase --concurrency 2
paicli sessions --workspace examples/codebase
paicli audit --workspace examples/codebase
```

Run `index` before searching. Indexing rebuilds the local index; changed or deleted files are excluded from search results until the next rebuild. Python functions/classes use AST boundaries, retaining decorators and nested locations. Uncovered module code, including imports and constants, is also indexed. Blocks use at most 100 source lines. JSX, MJS, CJS, CSS, HTML and other supported text files use line windows. Retrieval is lexical and needs no embedding model.

The default provider is `scripted`, which requires `--script` or a script for each plan task. It never silently replaces a failed remote model. All successful commands emit JSON; `--json` is accepted explicitly. JSON output uses ASCII-safe Unicode escapes for locale-independent pipes; parsing restores the original Unicode text. Use `paicli <command> --help` for options.

### Inspect and approve a write

```sh
paicli agent "Propose a review note" --workspace examples/codebase --script examples/write-proposal.json --interactive-approval
```

The CLI displays the complete diff. Type `approve <full-64-character-fingerprint>` exactly as prompted. Without the flag, a TTY, or the exact response, the write is denied. If the target changes after preview or the deadline expires while waiting, it is not replaced. A successful write uses an atomic file replacement.

The example's final sentence is scripted; inspect the tool `trace` and file contents to determine whether approval succeeded. An Agent can finish normally after reporting a denied tool, so exit code `0` alone does not prove that every tool succeeded.

## Bilingual code retrieval

**Experimental status: 3 of 4 predefined quality targets passed; overall quality acceptance did not pass. Reliable abstention is not established.** On a new 32-case set first evaluated after the v3 implementation/rules freeze, v3 reached gated Recall@5 **0.675** and MRR@5 **0.666667**. It rejected **6/20** answerable cases (**30%**, above the **25%** target) and returned snippets for **3/12** unanswerable cases, down from frozen v2's **4/12**. Gated recall was unchanged from v2. These are same-repository, agent-annotated retrieval results; raw candidate scores and generated-answer quality are separate measurements.

```sh
paicli index --workspace .
paicli retrieve "压缩上下文" --workspace . --limit 5
paicli retrieve "QuantumNimbus network protocol" --workspace .
```

`retrieve` and the read-only Agent tool `retrieve_code` use a fixed computing-term glossary, identifier splitting, and concept-level lexical scores. For ordinary queries, v3 preserves the v2 **required anchors** and **soft terms** policy: backtick identifiers, underscore/mixed-case identifiers, uppercase acronyms, non-initial unknown Titlecase words, and non-Han letter runs in mixed-Han queries become anchors. For short queries with at most **3 distinct non-stopword concepts**, every remaining unknown literal is also required. Other unknown prose remains ranking evidence without an individual veto. A candidate must contain every anchor and cover at least **0.6** of the known concepts plus anchors. With neither a known concept nor an anchor, the gate rejects the query.

Anchor matching uses normalized exact identifiers or contiguous components within one identifier, without assembling separate identifiers; Han literal anchors use substring matching. Explicit anchors are recognized before stopword filtering, so `GET` remains required even though ordinary `get` is a stopword. Anchor-only queries such as `` `get` `` can create candidates; when no concept matches, their ranking sums per-anchor BM25 scores using binary frequency. The threshold is a heuristic, not a correctness probability.

V3 adds generic bilingual **checkpoint, expiration/TTL/retention, cleanup, watch, and background/daemon** word families as `required_behaviors`. All recognized behaviors must have identifier evidence in the **same visible candidate and execution scope**. These families and their small test fixtures describe retrieval rules; PaiCLI has no added checkpoint, timed-expiry cleanup, or file-watcher business feature.

The behavior layer parses Python ASTs and records non-stub function names, loaded names/attributes, call targets/keywords, and literal-`True` daemon settings from complete visible source lines. Comments, docstrings, other strings, imports, class names, parameters, annotations/type aliases, placeholder bodies, and `TYPE_CHECKING` branches supply no behavior evidence. Module/class bodies, functions, lambdas, and comprehensions have separate scopes: a comprehension's first iterable stays in its enclosing scope, while its body, filters, and later iterables use the comprehension scope. Boolean expressions retain potentially evaluated prefixes and omit statically short-circuited operands; a `daemon=` keyword or `.daemon` assignment supplies positive setting evidence only with literal `True`. Each file has a **100,000 AST-node** budget. Non-Python files, parse failures, budget excesses, and analysis failures conservatively supply no behavior evidence; ordinary retrieval remains available.

For behavior queries, v3 gates the **complete ranked candidate pool**, then returns the first `k` accepted `hits`. Diagnostic raw `candidates` still contain only the first `k` lexical candidates, so an accepted hit can fall outside that raw top `k`. Ordinary queries retain v2's limit-before-gate policy. `profile.behavior_policy` records this selection rule. Results expose `required_behaviors`, matched/missing behaviors, identifier locations/scopes, analysis status, concepts, anchors, soft terms, coverage, rule/snapshot fingerprints, and an `accepted` or `insufficient_evidence` decision. Only accepted snippets reach the CLI/Agent.

An insufficient result means the retriever found inadequate evidence. It does not prove a feature is absent or force the whole Agent to abstain: `search_code` and other tools remain available. Unknown behavior wording and aliases, cross-function implementations, and non-Python code can cause false abstention. An identifier in an executable syntax position still does not establish runtime semantics or correctness; a matching expiration name, for example, does not verify a requested seven-day duration. Soft prose can also accept related code without supporting the requested behavior. This is an experimental lexical baseline with no embedding model; the original `search` behavior is unchanged.

The snapshot accepts at most 10,000 indexed chunks and 8 MiB of chunk content, with a separate 8 MiB source budget and 512 KiB per file. Candidates contain complete source lines within an 8,000-character window; oversized single lines are omitted and counted. Source hashes and workspace path policy are rechecked. The tool constructs its snapshot per call; offline evaluation builds one snapshot and reports construction time separately. Local work uses cooperative deadline checks.

## Cross-file analysis and offline evaluation

```sh
paicli graph --workspace . --query ToolRegistry
paicli graph --workspace . --symbol SYMBOL_ID --direction callers --depth 1
paicli evaluate examples/evaluation/cases.json --workspace . --k 5
paicli evaluate examples/evaluation/holdout.json --workspace . --k 5
paicli evaluate examples/evaluation/holdout-v2.json --workspace . --k 5
paicli evaluate examples/evaluation/holdout-v3.json --workspace . --k 5
```

Use the exact returned symbol `id` as `SYMBOL_ID`. With no query, `graph` emits its snapshot. Agent and read-only workers also receive `find_symbol`, `callers`, and `callees`. Each query rebuilds the graph without writing session state and includes source locations/digests. Direct, alias and relative imports can form resolved edges. Uncertain instances, inheritance, shadowing and dynamic dispatch remain unknown. Inspect `diagnostics`, `truncated`, and `analysis_incomplete`; an empty result cannot prove absence.

This is a static declaration graph, not source execution, full Python type inference, or runtime tracing. Limits: 5,000 safe files, 8 MiB total source, 10,000 symbols, 30,000 calls, 512 KiB and 100,000 AST nodes per file. Tool deadline checks are cooperative; local analysis is not forcibly preempted.

The existing **30/24/24-case sets are now development-visible data**: `cases.json` has 26 answerable and 4 unanswerable cases; `holdout.json` and `holdout-v2.json` each have 16 answerable and 8 unanswerable cases. Their historical first-look results and annotation audits remain available. On the former v2 holdout, v3 reduced unanswerable false retrievals from **3/8 to 0/8**, retaining **14/16** accepted answerable cases, Recall@5 **0.75**, and MRR@5 **0.729167**. That is a development regression result, not fresh held-out evidence.

For this iteration, a separate source-reading agent prepared [holdout-v3.json](examples/evaluation/holdout-v3.json): **32 new cases, 20 answerable and 12 unanswerable, with 20 Chinese and 12 English queries**. Implementation, rules, and four targets were frozen before the first v2/v3 comparison on this set. The [annotation audit](examples/evaluation/holdout-v3-annotations.json) and [measured results and freeze record](examples/evaluation/results-v3.json) preserve its provenance, hashes, and results.

| Method on the new v3 set | Recall@5 | MRR@5 | Answerable without snippets | Unanswerable returning snippets |
| --- | --- | --- | --- | --- |
| BM25, both versions | 0.7 | 0.725 | 4/20 | 9/12 |
| Graph expansion, both versions | 0.725 | 0.725 | 4/20 | 9/12 |
| Raw bilingual, both versions | 0.95 | 0.891667 | 0/20 | 12/12 |
| Frozen v2 gate | 0.675 | 0.666667 | 6/20 | 4/12 |
| V3 gate | 0.675 | 0.666667 | 6/20 | 3/12 |

| Predefined v3 quality target | Measured result | Status |
| --- | --- | --- |
| Answerable false-abstention rate ≤25% | 6/20 = 30% | **Failed** |
| Gated recall ≥80% of frozen v2 | 0.675/0.675 = 100% | Passed |
| Unanswerable false-retrieval rate ≤25% | 3/12 = 25% | Passed |
| Fewer negative false retrievals than nonzero v2 | 4/12 → 3/12 | Passed |

**Quality acceptance partially passed; overall acceptance failed.** The only acceptance change was the file-watcher debounce question `v3-019`, which changed from accepted to rejected. All six answerable false abstentions already occurred with frozen v2. Three unanswerable cases still returned snippets: HTTP 429 initial backoff/jitter, an FTS synonym schema, and a tokenizer/model price table. Their required details remained soft wording. Raw scores measure the lexical candidate list, not the actual `retrieve` output; behavior-query hits can draw from beyond raw top `k` under the selection policy above.

The new set has **5 queries that trigger `required_behaviors`, all unanswerable, and no behavior-positive query**. It therefore does not measure positive recall for implemented behaviors; paired runnable unit cases supply separate contract evidence. The first-look result does not establish reliable abstention.

All datasets use same-repository, agent-generated source annotations, without independent human annotation or cross-project evaluation. The new 32-case set supplies first-look evidence for this frozen implementation and becomes development-visible after this run. Only manifest source enters a temporary corpus; questions, labels, README files and old reports are excluded. Changed digests fail explicitly: review labels before refreshing the manifest.

Reports compare `bm25`, `graph_expand` (resolved one-hop calls), `code_aware` (raw bilingual candidates), and `code_aware_gated` (accepted candidates). They retain each query, visible evidence, decision, analysis profile, Recall@k, MRR@k, false retrieval on unanswerable cases, and category metrics. Overall Recall/MRR include **all answerable cases**, counting rejected ones as zero; false-abstention rate and conditional recall among accepted answerable cases expose the tradeoff. Undefined denominators are `null`. Raw and gated bilingual results share one measured search; build times are separate. Span overlap measures retrieved evidence, not answer correctness; broad class/function labels can inflate coverage. Unanswerable false retrieval means snippets were returned, not model hallucination. Neither graph expansion nor the gate guarantees better ranking or answer quality.

Answer evaluation defaults to `not_requested`. Only this explicit command contacts a provider, after setting `OPENAI_API_KEY` in the process environment:

```sh
paicli evaluate examples/evaluation/cases.json --workspace . \
  --provider openai --model YOUR_MODEL --method bm25 --timeout 20 --repeats 1
```

Missing configuration produces `not_run` and exit code 1. Protocol, format, citation and timeout failures remain individual failed attempts, with no scripted fallback. Checks cover JSON schema, supplied evidence IDs and abstention; **semantic correctness remains pending human review**. An empty citation list merely references no invalid IDs and does not establish support. Unavailable usage/cost are `null`. Recorded model checks use local HTTP fixtures; no public-model quality result is claimed.

## Connect a model or FlowTrail

Credentials are read from process environment variables only. `.env.example` contains placeholders; the CLI does not automatically load `.env` files.

```sh
export OPENAI_API_KEY="<your-api-key>"
paicli agent "Inspect refund idempotency" --workspace examples/codebase --provider openai --base-url https://api.openai.com/v1 --model YOUR_MODEL

export FLOWTRAIL_API_TOKEN="<your-issued-access-token>"
paicli doctor http://127.0.0.1:8000/healthz
paicli platform me --base-url http://127.0.0.1:8000
paicli platform quota --base-url http://127.0.0.1:8000
paicli platform runs --base-url http://127.0.0.1:8000
paicli platform run RUN_ID --base-url http://127.0.0.1:8000
paicli platform events RUN_ID --after 3 --base-url http://127.0.0.1:8000
```

In PowerShell, set variables with `$env:OPENAI_API_KEY = "<your-api-key>"` and `$env:FLOWTRAIL_API_TOKEN = "<your-issued-access-token>"` instead of `export`.

The five platform commands send `Authorization: Bearer …` to the corresponding FlowTrail read-only API and do not print the token. `events` fetches history with `follow=false`, sends `Last-Event-ID` for `--after`, and emits `{events, cursor}` JSON. Identity, quota arithmetic, run/node structure, and SSE event IDs are validated. Invalid, out-of-order, incomplete, oversized, or over-4,096-event responses fail explicitly rather than return partial success. Responses are limited to 1 MiB, with a default total request deadline of 10 seconds, including streamed chunks.

Supply an unexpired access token issued by your FlowTrail instance. Expired/revoked credentials return 401; permission errors and foreign resource IDs remain failures. `doctor` independently reports DNS, TCP, and HTTP checks, using `/healthz` by default. Credentialed remote endpoints require HTTPS; loopback HTTP is supported for local development. Redirects are not followed. HTTP errors omit response bodies and credentials.

### Reproduce the two-project integration

Clone `flowtrail-python` beside this repository and install both projects into one Python environment:

```sh
python -m pip install -e . -e ../flowtrail-python
python scripts/integration_flowtrail.py --flowtrail-src ../flowtrail-python/src
```

The script starts a real local FlowTrail process with a temporary database and random temporary tokens. It exercises all five CLI queries, object quota changes, idempotent submission, cursor replay, invalid credentials, viewer restrictions and cross-tenant reads. The workflow contains explicitly labeled mock inference. It prints a check summary and attempts to clean up the process and database. This is a separate integration gate, in addition to the standalone unit suite; see the current verification status below.

Remote models receive the supplied conversation and tool results, which can include workspace source text. Session messages are stored locally in plaintext at `.paicli/state.sqlite3`; that directory is ignored by Git.

## Limits and execution boundaries

- Agent defaults: 8 rounds, 16 calls per round, a 24,000-character serialized context budget, and a 60-second time budget. The character budget is not a tokenizer. Older whole turns are replaced with deterministic excerpts; the current turn must fit.
- Plan defaults: concurrency `2` (allowed `1–8`) and a 120-second time budget. Duplicate IDs, missing dependencies, and cycles are rejected before workers start. Workers cannot write files. A failure cancels outstanding tasks and stops dependent work.
- Asynchronous model calls and plan tasks support cancellation. Local filesystem operations and interactive input use cooperative deadline checks; a blocked human prompt cannot be forcibly interrupted by `asyncio`.
- Paths are restricted to the workspace. Parent traversal, absolute paths, protected paths, symbolic links, Windows reparse points/device names, and hard-linked files are rejected. These are application checks, **not an OS sandbox**; hostile concurrent filesystem changes require OS/container isolation.
- File reads and indexed files are capped at 512 KiB; proposed writes at 256 KiB. Indexing scans up to 5,000 allowed files. These defaults target local codebases; no large-scale throughput claim is made.
- `audit` checks POSIX world-write mode bits without changing permissions. It does not analyze Windows ACLs.

This release does not implement MCP, vector retrieval, Ollama-specific or streaming adapters, arbitrary shell execution, automatic model-generated plans, a Reviewer/repair loop, or full Java feature parity.

## Verification and layout

```sh
python -m unittest discover -s tests -v
paicli demo --json
```

Local verification on **2026-10-23**, Windows / Python 3.12.14: **186 tests passed, 0 failed, 0 skipped**, in **22.854 seconds**; the offline demo also passed. New coverage exercises bilingual behavior families, visible-line and same-scope identifier evidence, comments/strings/declarations/stubs/type-checking exclusions, comprehension scope, Boolean short-circuit prefixes, literal-`True` daemon evidence, behavior analysis budgets/failures, and gate-before-limit selection. Existing coverage includes v2 anchor/soft-term rules, source/snapshot consistency, CLI/scripted-Agent round trips, platform/SSE boundaries, write approvals, context budgets, DAG concurrency/cancellation, and subprocess encodings. Engineering tests and retrieval quality have separate acceptance results.

The real local FlowTrail integration passed against the CI-pinned dependency revision `9249d59135009434229ba4525f124c53e5553ff8`, exported read-only from local Git objects, with explicitly mocked inference. Two attempts against the current uncommitted sibling completed business checks but failed during `TemporaryDirectory` cleanup with `PermissionError`; those attempts are recorded as failures. Public-model answer quality and Docker builds were not validated. CI covers Ubuntu/Windows with Python 3.11/3.12; see actual workflow runs for remote results.

`src/paicli/` contains the CLI, Agent, tools, storage, index, planner, and API adapters. `tests/` contains the standard-library test suite; `examples/` contains inspectable fixtures and scripted responses. A Dockerfile is included for subsequent container validation.

Exit codes: `0` completed command; `1` local I/O/database error, failed diagnostic check, or explicit answer evaluation not run/containing errors; `2` invalid configuration or policy/model/platform/plan error; `3` exhausted execution budget; `130` interruption. Review tool-level results separately.

## Origin and license

The design draws on the PaiCLI concepts studied in the existing [Java FlowTrail CLI](https://github.com/cloudwallker/flowtrail-cli). This repository's Python source was written with AI coding assistance and represents a focused implementation, not a line-by-line port. See [NOTICE](NOTICE) for the source revision and attribution. The inherited [MIT license](LICENSE) and original contributor notice are retained.
