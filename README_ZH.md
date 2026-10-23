# PaiCLI Python

**面向代码分析与 AI 平台运维的 Python CLI，提供可追踪工具调用、精确写入审批和可重复的离线演示。**

[English](README.md) | 简体中文

项目将模型工具循环、本地代码检索、会话保存和平台只读诊断组合到一个命令行程序中。它是用于学习、展示和继续完善的 Python 实现。

![真实离线演示输出排版图：代码索引、Agent 工具调用、写入拒绝和 DAG 执行](docs/images/demo.png)

*由真实离线演示输出排版生成，并非终端截图。模型回复采用明确标注的固定脚本；文件工具、SQLite 存储、检索和任务调度均执行真实代码。*

## 快速开始

需要 **Python 3.11+**，且其 SQLite 支持 FTS5。从仓库根目录执行：

```sh
python -m venv .venv
# Linux/macOS：source .venv/bin/activate
# Windows PowerShell：.\.venv\Scripts\Activate.ps1
python -m pip install .
paicli demo --json
```

`demo` 不需要 API Key 或网络连接。它会创建临时工作区、索引 Python 样例、执行两次 Agent 工具调用、验证未审批写入被拒绝，再运行三节点 DAG，最后清理临时目录。退款样例仅演示进程内幂等，不是生产支付代码。

## 当前实现

| 能力 | 实现方式 |
| --- | --- |
| Agent | ReAct 风格的请求、工具执行、结果回填循环；非流式 OpenAI 兼容接口与显式脚本模型 |
| 工具 | `read_file`、`list_files`、`search_code`、`retrieve_code`、`find_symbol`、`callers`、`callees`、`write_file`；参数类型及工作区路径校验 |
| 写入审批 | 展示完整 diff，以相对路径、原内容和新内容计算 SHA-256 指纹；每次写入单独精确审批 |
| 会话 | SQLite 保存 system/user/assistant/tool 消息；支持会话 ID 续接及旧轮次的确定性摘录 |
| 代码检索 | Python AST 与导入/常量/顶层代码、JSX/MJS/CSS/HTML，限制行窗口，FTS5/BM25 排名、源码位置和过期结果过滤 |
| 双语检索 | 实验性中英文概念匹配、标识符拆词、确定性词法排序和明确的证据不足结果 |
| 任务计划 | DAG 校验、`asyncio` 有界并发、依赖结果传递、失败取消，以及只读 Agent Worker |
| 平台运维 | DNS/TCP/HTTP 诊断、FlowTrail 身份/配额/任务列表/详情/事件回放，以及只读 POSIX 全局可写权限检查 |

## 运行完整示例

```sh
paicli index --workspace examples/codebase
paicli search "refund_order request_key" --workspace examples/codebase
paicli agent "分析退款幂等实现" --workspace examples/codebase --script examples/read-and-search.json --session refund-review
paicli plan examples/plan.json --workspace examples/codebase --concurrency 2
paicli sessions --workspace examples/codebase
paicli audit --workspace examples/codebase
```

先执行 `index`，再进行检索。索引采用重建方式；文件改变或删除后，搜索会过滤旧结果，重建后再使用新内容。Python 函数/类按 AST 边界切分，保留装饰器和嵌套定义位置；未被这些节点覆盖的导入、常量及顶层代码也会入索引。每块最多 100 个源码行。JSX、MJS、CJS、CSS、HTML 等受支持文本使用行窗口。这里是词法检索，不依赖 Embedding 模型。

默认 provider 为 `scripted`，必须通过 `--script` 或计划任务的 `script` 提供回复序列。远程模型失败时不会自动降级成脚本模型。成功命令默认输出 JSON，也接受显式 `--json`。JSON 输出使用兼容 ASCII 的 Unicode 转义，避免依赖管道的系统编码；解析后还原原始中文等 Unicode 文本。其他选项见 `paicli <命令> --help`。

### 查看并批准一次写入

```sh
paicli agent "提出一条评审记录" --workspace examples/codebase --script examples/write-proposal.json --interactive-approval
```

CLI 展示完整 diff 后，需要严格输入提示中的 `approve <完整64位指纹>`。未开启交互审批、标准输入不是 TTY，或输入不匹配，都会拒绝写入。预览后目标文件发生变化，或者等待批准期间超时，都不会替换文件。审批通过的写入采用原子文件替换。

示例末句来自固定脚本，应以工具 `trace` 和实际文件内容判断写入是否发生。Agent 可以在报告工具被拒绝后正常结束，因此退出码 `0` 不代表每个工具都执行成功。

## 中英文代码检索

**实验状态：工程契约测试及本轮两个预设可用性目标通过，尚未建立可靠拒答能力。** 实现冻结后，在全新 24 题留出集上首次评测，v2 门槛的 Recall@5 为 **0.75**、MRR@5 为 **0.729167**，误拒 **2/16** 道有答案题，在 **3/8** 道无答案题上返回了片段。同一新集上的冻结 v1 门槛误拒全部 16 道有答案题、无答案误召回为 0。两版本的双语原始候选 Recall@5 均为 **0.9375**，v2 保留了原始召回的 **80%**。raw 成绩描述过滤前候选，不能写成 `retrieve` 的实际效果。

```sh
paicli index --workspace .
paicli retrieve "压缩上下文" --workspace . --limit 5
paicli retrieve "QuantumNimbus network protocol" --workspace .
```

`retrieve` 命令和只读 Agent 工具 `retrieve_code` 使用固定计算机术语表、标识符拆词及概念级词法分数。v2 将查询拆成**必需锚点**与**软词**：反引号标识符、下划线/混合大小写标识符、大写缩写、非首个词段中未识别的 Titlecase 词，以及含汉字查询中的非汉字母词段会成为锚点。对去停用词后最多 **3 个不同概念**的短查询，剩余未知字面词也全部必需；其他未知自然语言措辞继续影响排序，但不逐词否决。候选必须包含全部锚点，并覆盖“已知概念＋锚点”的至少 **0.6**；两者均为空时拒绝查询。

锚点按规范化后的完整标识符或同一标识符内的连续组成部分匹配，不跨标识符拼接；汉字字面锚点按子串匹配。显式锚点先于停用词过滤识别，因此普通 `get` 虽是停用词，大写 `GET` 仍为必需项。仅含锚点的查询如 `` `get` `` 也能产生候选；没有概念匹配时，按各锚点的二值词频 BM25 分数之和排序。阈值是启发式规则，不是答案正确概率。结果展示概念、锚点、软词、已匹配/缺失锚点、证据覆盖率、规则/源码快照指纹，以及 `accepted` 或 `insufficient_evidence` 决策。CLI/Agent 只接收通过门槛的片段；被拒绝的原始候选只供离线对照评测。

证据不足仅表示这次检索没有找到足够的词法证据，不能证明功能不存在，也不能保证整个 Agent 拒答，因为 `search_code` 等工具仍然可用。未知措辞可能误拒绝有答案题，软词也可能使相邻功能代码被接受，却不能支持所问行为；名称或注释匹配不等于语义证据成立。这是实验性词法基线，没有接入 Embedding；原有 `search` 行为保持不变。

快照最多接受 10,000 个索引块、8 MiB 索引内容，另有 8 MiB 源码总预算和单文件 512 KiB 上限。候选只包含 8,000 字符窗口内的完整源码行；超长单行省略并计数。重新检查源码摘要与工作区路径权限。工具每次调用构建快照，离线评测复用一次构建并单列耗时；本地处理采用协作式截止检查。

## 跨文件分析与离线评测

```sh
paicli graph --workspace . --query ToolRegistry
paicli graph --workspace . --symbol SYMBOL_ID --direction callers --depth 1
paicli evaluate examples/evaluation/cases.json --workspace . --k 5
paicli evaluate examples/evaluation/holdout.json --workspace . --k 5
paicli evaluate examples/evaluation/holdout-v2.json --workspace . --k 5
```

`SYMBOL_ID` 使用上一步返回的完整 `id`。`graph` 不带查询参数时输出图快照；`find_symbol`、`callers`、`callees` 同时提供给 Agent 和只读 Worker。每次重新构图，不写入会话数据库；图含源码位置和摘要。跨文件、别名及相对导入可建立确定边；不明确实例、继承、名称遮蔽与动态派发保守保留为未知。扫描失败和截断通过 `diagnostics`、`truncated`、`analysis_incomplete` 展示，不能把空结果当作不存在的证明。

这是一份静态声明关系图，不运行源码，也不承诺完整 Python 类型推断或运行时调用追踪。构图最多扫描 5,000 个安全文件、8 MiB 总源码、10,000 个符号、30,000 条调用边；单文件 512 KiB、100,000 个 AST 节点。工具的截止检查是协作式的，本地构图不会被强制抢占。

`cases.json` 含 **30 道开发题：26 题有答案、4 题无答案**，限定 10 个源码文件及其 SHA-256。旧 `holdout.json` 含 **24 题：16 题有答案、8 题无答案，其中 16 题为中文**，曾用于 v1 首次留出评测，原标注审计保留在 `holdout-annotations.json`。这两套旧数据在 v2 开发期间均已可见，现在都属于开发数据。旧集首次 v1 评测的 raw Recall@5 为 0.90625、BM25 为 0.42708，但门槛误拒全部 16 道可答题；这些历史成绩不是本轮 v2 新集结果。

本轮由不参与实现的源码阅读代理基于 9 个稳定源码文件构造全新 **`holdout-v2.json`：24 题，16 题有答案、8 题无答案，包含 16 题中文、8 题英文**。首次评测前冻结实现、规则及两个可用性目标：可答误拒率不超过 **25%**，门槛后的召回至少保留 raw 的 **80%**。v2 实测分别为 **12.5%** 与 **80%**，两项目标通过；但 **3/8** 的无答案误召回仍不足以宣称可靠拒答。新标注审计与实际基准摘要分别见 [holdout-v2-annotations.json](examples/evaluation/holdout-v2-annotations.json) 和 [results-v2.json](examples/evaluation/results-v2.json)。

| 新 v2 集上的方法 | Recall@5 | MRR@5 | 可答题无片段 | 无答案返回片段 |
| --- | --- | --- | --- | --- |
| BM25 | 0.625 | 0.614583 | 4/16 | 4/8 |
| 图扩展 | 0.625 | 0.595833 | 4/16 | 4/8 |
| 双语原始候选，v1/v2 | 0.9375 | 0.786458 | 0/16 | 8/8 |
| 冻结 v1 门槛 | 0 | 0 | 16/16 | 0/8 |
| V2 门槛 | 0.75 | 0.729167 | 2/16 | 3/8 |

所有数据集均为代理依据源码标注，未经独立人工标注，也没有开展跨项目评测。新集仅为本次冻结实现的首次留出证据，后续迭代时也将成为开发可见数据。只把清单源码复制到临时工作区，题目、标签、README 和历史报告不会进入索引；源码变更导致摘要不符时直接拒绝。应先重新核对标签，再更新摘要，不能盲目刷新。

报告对照 `bm25`、`graph_expand`（一跳确定调用关系）、`code_aware`（双语原始候选）和 `code_aware_gated`（通过门槛的候选），保留每题查询、可见证据、决策、分析配置、Recall@k、MRR@k、无答案误召回及分类指标。总体 Recall/MRR 的分母包含**全部有答案题**，被拒绝题计 0；另报可答误拒率与已接受可答题的条件召回，展示取舍。无分母返回 `null`。双语 raw/gated 共用一次真实检索计时，构建耗时单列。相关性按标注行段与实际返回证据重叠计算，宽泛标注可能使指标偏乐观；无答案误召回只表示返回片段，不等于模型幻觉。图扩展与门槛均不保证提高排名或回答质量。

默认只做离线检索，回答评测标为 `not_requested`。显式调用以下命令才访问模型，需要先在进程环境设置 `OPENAI_API_KEY`：

```sh
paicli evaluate examples/evaluation/cases.json --workspace . \
  --provider openai --model YOUR_MODEL --method bm25 --timeout 20 --repeats 1
```

缺配置返回 `not_run` 并退出 1；协议、格式、非法引用或超时保留逐题失败，不回退脚本模型。只检验 JSON 格式、证据 ID 合法性及拒答，**语义正确率始终标记待人工复核**。空引用列表仅表示未引用非法 ID，不证明答案有证据支持。用量和费用拿不到时为 `null`；本轮验证使用本地 HTTP fixture，没有公网模型成绩。

## 接入模型与 FlowTrail

凭据只从进程环境变量读取。`.env.example` 仅提供占位说明，CLI 不会自动加载 `.env` 文件。

```sh
export OPENAI_API_KEY="<你的API密钥>"
paicli agent "分析退款幂等实现" --workspace examples/codebase --provider openai --base-url https://api.openai.com/v1 --model YOUR_MODEL

export FLOWTRAIL_API_TOKEN="<平台签发的访问令牌>"
paicli doctor http://127.0.0.1:8000/healthz
paicli platform me --base-url http://127.0.0.1:8000
paicli platform quota --base-url http://127.0.0.1:8000
paicli platform runs --base-url http://127.0.0.1:8000
paicli platform run RUN_ID --base-url http://127.0.0.1:8000
paicli platform events RUN_ID --after 3 --base-url http://127.0.0.1:8000
```

PowerShell 中将 `export` 替换为 `$env:OPENAI_API_KEY = "<你的API密钥>"` 和 `$env:FLOWTRAIL_API_TOKEN = "<平台签发的访问令牌>"`。

五类平台命令使用 `Authorization: Bearer …` 访问对应的 FlowTrail 只读 API，不打印令牌。`events` 使用 `follow=false` 获取历史，按 `--after` 发送 `Last-Event-ID`，输出 `{events, cursor}` JSON。客户端校验身份、配额算术关系、任务/节点结构与 SSE 序号；无效、乱序、不完整、超大或超过 4,096 条事件的响应会明确失败，不返回部分成功。响应上限为 1 MiB，默认总请求截止时间 10 秒，包含流式分块的等待。

令牌应由本地 FlowTrail 签发且仍有效。过期/撤销返回 401；权限不足及其他租户资源仍作为失败处理。`doctor` 分别返回 DNS、TCP 和 HTTP 检查结果，默认健康路径为 `/healthz`。携带凭据的远程端点必须使用 HTTPS，本机回环地址可使用 HTTP 进行开发联调。客户端不跟随重定向，HTTP 失败信息不包含响应正文或凭据。

### 复现两个项目的联合演示

将 `flowtrail-python` 克隆到本仓库的同级目录，在同一个 Python 环境中安装两项目：

```sh
python -m pip install -e . -e ../flowtrail-python
python scripts/integration_flowtrail.py --flowtrail-src ../flowtrail-python/src
```

脚本启动真实本地 FlowTrail 进程，使用临时数据库和随机临时令牌，实测五类 CLI 查询、对象配额变化、幂等提交、游标回放、错误令牌、viewer 权限及跨租户读取。工作流包含明确标记的 mock 推理；输出检查摘要，并清理进程和数据库。这是独立于单仓库单元测试的额外集成检查。

远程模型会收到会话和工具结果，其中可能包含工作区源码。完整会话明文保存在本地 `.paicli/state.sqlite3`，该目录已加入 Git 忽略规则。

## 默认限制与执行边界

- Agent 默认最多 8 轮、每轮 16 个工具调用、24,000 字符的序列化上下文预算和 60 秒时间预算。字符预算不是精确 Token 计数；较旧的完整轮次会替换为确定性摘录，当前轮次必须能够装入预算。
- Plan 默认并发度 `2`，允许 `1–8`，时间预算为 120 秒。重复 ID、缺失依赖和环会在 Worker 启动前拒绝。Worker 不允许写文件；任务失败后取消未完成任务，并停止后续依赖。
- 异步模型调用和计划任务支持取消。本地文件操作与人工输入采用协作式截止检查；`asyncio` 无法强制打断阻塞中的人工输入。
- 工作区路径检查拒绝 `..`、绝对路径、受保护目录、符号链接、Windows reparse point/设备名和硬链接文件。这是应用层约束，**不是操作系统沙箱**；应对恶意并发文件系统变更，需要 OS 权限或容器隔离。
- 单个读取/索引文件上限为 512 KiB，单次写入上限为 256 KiB；索引最多扫描 5,000 个允许访问的文件。当前面向本地代码库，没有宣称大规模吞吐能力。
- `audit` 只读检查 POSIX 全局可写权限位，不执行 `chmod`；Windows 下不分析 ACL。

当前版本未实现 MCP、向量检索、Ollama 专用或流式适配器、任意 Shell、模型自动生成计划、Reviewer/自动修复循环，也不代表 Java 版本的全功能迁移。

## 验证与目录

```sh
python -m unittest discover -s tests -v
paicli demo --json
```

**2026-10-23** 本地验证环境为 Windows / Python 3.12.14：**157 项测试通过，0 失败、0 跳过**，完整运行耗时 **17.813 秒**。新增 v2 锚点/软词、严格短查询、`GET` 停用词优先级、仅锚点检索、双语/Unicode 检索、源码和快照一致性、损坏索引处理、误拒率口径，以及真实 CLI/脚本 Agent 的工具往返契约。既有覆盖包括源码行号与模块/JSX 检索、过期索引、平台响应结构、SSE 游标/完整性/限额/截止时间与认证错误控制帧、路径及链接边界、写入审批、上下文、DAG 并发取消及不同子进程编码。测试通过不等于检索效果通过。本轮离线 demo 与真实本地 FlowTrail 联调均通过；联调使用本地同级项目及显式 mock 推理。尚未开展公网模型回答质量评测和 Docker 构建验证。CI 覆盖 Ubuntu/Windows、Python 3.11/3.12，远端结果以实际 workflow 运行为准。

`src/paicli/` 包含 CLI、Agent、工具、存储、检索、计划调度和 API 适配；`tests/` 使用标准库测试框架；`examples/` 提供可查看的源码样例与脚本回复。Dockerfile 供后续容器验证使用。

退出码：`0` 命令结束；`1` 本地 I/O、数据库错误、诊断检查失败，或显式回答评测未执行/含失败；`2` 配置、策略、模型、平台或计划错误；`3` 执行预算耗尽；`130` 用户中断。工具级结果需要单独查看。

## 来源与许可

设计参考了既有 [Java FlowTrail CLI](https://github.com/cloudwallker/flowtrail-cli) 中研究和实践的 PaiCLI 思路。本仓库 Python 源码使用 AI 编程辅助编写，是聚焦核心能力的独立实现，并非逐行翻译。参考版本及署名说明见 [NOTICE](NOTICE)。沿用原有 [MIT 许可证](LICENSE)，保留原贡献者声明。
