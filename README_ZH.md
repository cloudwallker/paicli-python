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
| 双语检索 | 实验性中英文概念匹配、标识符拆词、确定性词法排序、限定作用域的 Python 标识符证据及明确的证据不足结果 |
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

**实验状态：v4 改善了已知开发回归，但新集泛化验收未达，3 个预设目标仅通过 1 个，尚未建立可靠拒答能力。** 原 32 题集已为开发数据，v4 将可答误拒从 **6/20 降至 4/20**，无答案误召回从 **3/12 降至 0/12**。实现与规则冻结后，在新 24 题集上首次对照，v3/v4 均为 Recall@5 **0.5**、MRR@5 **0.5625**，误拒 **5/16（31.25%）**，负例误召回 **4/8（50%）**。这些是同项目、代理标注的检索结果，与模型生成回答质量分开衡量。

```sh
paicli index --workspace .
paicli retrieve "压缩上下文" --workspace . --limit 5
paicli retrieve "QuantumNimbus network protocol" --workspace .
```

`retrieve` 命令和只读 Agent 工具 `retrieve_code` 使用固定计算机术语表、标识符拆词及概念级词法分数。v4（`bilingual-concept-bm25-v4`）新增 **version（版本）** 和 **excerpt（摘录）** 两个普通概念，沿用 v2 的**必需锚点**、**软词**与 **0.6** 覆盖门槛：反引号标识符、下划线/混合大小写标识符、大写缩写、非首个词段中未识别的 Titlecase 词，以及含汉字查询中的非汉字母词段会成为锚点。对去停用词后最多 **3 个不同概念**的短查询，剩余未知字面词也全部必需；其他未知自然语言措辞继续影响排序，但不逐词否决。候选必须包含全部锚点，并覆盖“已知概念＋锚点”的至少 0.6；两者均为空时拒绝查询。

锚点按规范化后的完整标识符或同一标识符内的连续组成部分匹配，不跨标识符拼接；汉字字面锚点按子串匹配。显式锚点先于停用词过滤识别，因此普通 `get` 虽是停用词，大写 `GET` 仍为必需项。仅含锚点的查询如 `` `get` `` 也能产生候选；没有概念匹配时，按各锚点的二值词频 BM25 分数之和排序。阈值是启发式规则，不是答案正确概率。

行为门槛要求每个被识别的 `required_behaviors` 概念都在**同一可见候选、同一执行作用域**中具有标识符证据。v4 在既有 checkpoint、expiration/TTL/retention、cleanup、watch、background/daemon 词族上，新增 **jitter（抖动）、synonym（同义词）、tokenization（分词）、pricing（计价）** 四个窄中英词族，AST 证据规则不变。这些是检索词汇与测试小例，没有新增对应的 PaiCLI 业务功能。

行为层解析 Python AST，从完整可见源码行记录非占位函数名、读取位置的名称/属性、调用目标/关键字，以及字面量 `True` 的 daemon 设置。注释、docstring、其他字符串、导入、类名、形参、注解/类型别名、占位函数体和 `TYPE_CHECKING` 分支均不提供行为证据。模块/类体、函数、lambda 和生成式分别划分作用域：生成式首个迭代对象留在外层，其主体、过滤条件与后续迭代对象归入生成式作用域。布尔表达式保留可能执行的前缀，排除静态短路后的操作数；`daemon=` 关键字和 `.daemon` 赋值只在字面量 `True` 时提供启用设置证据。每文件限制 **100,000 个 AST 节点**；非 Python、解析失败、超限和分析失败时，该层保守不给出行为证据，普通检索仍可使用。

特定行为查询先对**完整排序候选池**过门槛，再截取前 `k` 个合格 `hits`。诊断用 raw `candidates` 仍只保留词法排名前 `k` 项，因此合格命中可能来自 raw 前 `k` 之外。普通查询沿用 v2 的先截取、后门槛规则；`profile.behavior_policy` 记录这项选择规则。结果展示 `required_behaviors`、匹配/缺失行为、标识符位置与作用域、分析状态、概念、锚点、软词、覆盖率、规则/快照指纹，以及 `accepted` 或 `insufficient_evidence` 决策。CLI/Agent 只接收通过门槛的片段。

证据不足表示这次检索没有找到足够证据，不能证明功能不存在，也不能保证整个 Agent 拒答，因为 `search_code` 等工具仍然可用。未知行为措辞与别名、跨函数实现及非 Python 代码可能造成误拒。标识符出现在可执行语法位置仍不证明运行语义或正确性，例如命中过期相关名称并未验证所要求的“七天”时长。软词也可能让相关代码通过，却不足以支持所问行为。这是没有接入 Embedding 的实验性词法基线，原有 `search` 行为保持不变。

快照最多接受 10,000 个索引块、8 MiB 索引内容，另有 8 MiB 源码总预算和单文件 512 KiB 上限。候选只包含 8,000 字符窗口内的完整源码行；超长单行省略并计数。重新检查源码摘要与工作区路径权限。工具每次调用构建快照，离线评测复用一次构建并单列耗时；本地处理采用协作式截止检查。

## 跨文件分析与离线评测

```sh
paicli graph --workspace . --query ToolRegistry
paicli graph --workspace . --symbol SYMBOL_ID --direction callers --depth 1
paicli evaluate examples/evaluation/cases.json --workspace . --k 5
paicli evaluate examples/evaluation/holdout.json --workspace . --k 5
paicli evaluate examples/evaluation/holdout-v2.json --workspace . --k 5
paicli evaluate examples/evaluation/holdout-v3.json --workspace . --k 5
paicli evaluate examples/evaluation/holdout-v4.json --workspace . --k 5
```

`SYMBOL_ID` 使用上一步返回的完整 `id`。`graph` 不带查询参数时输出图快照；`find_symbol`、`callers`、`callees` 同时提供给 Agent 和只读 Worker。每次重新构图，不写入会话数据库；图含源码位置和摘要。跨文件、别名及相对导入可建立确定边；不明确实例、继承、名称遮蔽与动态派发保守保留为未知。扫描失败和截断通过 `diagnostics`、`truncated`、`analysis_incomplete` 展示，不能把空结果当作不存在的证明。

这是一份静态声明关系图，不运行源码，也不承诺完整 Python 类型推断或运行时调用追踪。构图最多扫描 5,000 个安全文件、8 MiB 总源码、10,000 个符号、30,000 条调用边；单文件 512 KiB、100,000 个 AST 节点。工具的截止检查是协作式的，本地构图不会被强制抢占。

既有 **30/24/24/32 题集现均为开发可见数据**。原 32 题 `holdout-v3.json` 上，v4 将误拒 **6/20 → 4/20（20%）**、负例误召回 **3/12 → 0/12**，Recall@5 **0.675 → 0.775**，MRR@5 **0.666667 → 0.766667**。`holdout-v2.json` 保留 **14/16** 道可答题接受、Recall@5 **0.75** 和负例误召回 **0/8**，MRR@5 **0.729167 → 0.770833**；`cases.json` 与 `holdout.json` 的门槛指标均不变。这些是开发回归结果。v3 的[题集](examples/evaluation/holdout-v3.json)、[标注审计](examples/evaluation/holdout-v3-annotations.json)与[首次评测结果](examples/evaluation/results-v3.json)保留为历史记录。

本轮由另一源码阅读代理构造 [holdout-v4.json](examples/evaluation/holdout-v4.json)：**全新 24 题，16 题有答案、8 题无答案，中英文各 12 题**。在首次 v3/v4 对照前冻结源码、规则与三个目标。[标注审计](examples/evaluation/holdout-v4-annotations.json)与[实测结果及冻结记录](examples/evaluation/results-v4.json)保留了来源、摘要及测量结果。

| 新 v4 集上的方法 | Recall@5 | MRR@5 | 可答题无片段 | 无答案返回片段 |
| --- | --- | --- | --- | --- |
| BM25，两版本相同 | 0.61875 | 0.708333 | 3/16 | 7/8 |
| 图扩展，两版本相同 | 0.6625 | 0.703125 | 3/16 | 7/8 |
| 双语原始候选，两版本相同 | 0.841667 | 0.848958 | 0/16 | 8/8 |
| 冻结 v3 门槛 | 0.5 | 0.5625 | 5/16 | 4/8 |
| V4 门槛 | 0.5 | 0.5625 | 5/16 | 4/8 |

| v4 新集预设目标 | 实测结果 | 状态 |
| --- | --- | --- |
| 可答误拒率 ≤25% | 5/16 = 31.25% | **未通过** |
| 无答案误召回率 ≤25% | 4/8 = 50% | **未通过** |
| 门槛召回至少保留冻结 v3 的 80% | 0.5/0.5 = 100% | 通过 |

**开发目标通过，新集泛化验收未达。** 新集仅有 **1 道无答案题命中新增 version 概念**；其余五个新增概念 excerpt、jitter、synonym、tokenization、pricing 的**查询覆盖均为 0**，新增普通概念没有正例，新增行为词族没有任何题目覆盖。因此该新集不能验证本轮新增规则的正向泛化，成对可运行测试提供单独的工程证据。

所有数据集均为同项目、代理依据源码标注，未经独立人工标注，也没有开展跨项目评测。新 24 题集本轮后也成为开发可见数据。只把清单源码复制到临时工作区，题目、标签、README 和历史报告不会进入索引；源码变更导致摘要不符时直接拒绝。应先重新核对标签，再更新摘要，不能盲目刷新。

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

脚本启动真实本地 FlowTrail 进程，使用临时数据库和随机临时令牌，实测五类 CLI 查询、对象配额变化、幂等提交、游标回放、错误令牌、viewer 权限及跨租户读取。工作流包含明确标记的 mock 推理；输出检查摘要并尝试清理进程和数据库。这是独立于单仓库单元测试的额外集成检查，本轮状态见下方验证记录。

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

**2026-10-23** 本地验证环境为 Windows / Python 3.12.14：**196 项测试通过，0 失败、0 跳过**，完整运行耗时 **25.991 秒**；离线 demo 与独立审查的 **79 项别名/边界检查**通过。新增测试为普通概念与行为词族提供成对正负例；既有覆盖包括可见行/同作用域 AST 证据与排除项、短路/生成式/daemon 边界、分析限额、先门槛后截取、v2 锚点、CLI/Agent 工具、平台/SSE 边界、写入审批、上下文预算及 DAG 取消。工程测试与检索质量分别验收。

本轮对 CI 固定依赖 FlowTrail `9249d59135009434229ba4525f124c53e5553ff8` 执行原集成命令时，首次返回通用错误，根因尚未确定。随后跟踪诊断调用同一 demo 函数，完整通过 **12 类检查**、**7 条事件**及清理，推理使用显式 mock。两项结果均保留，不能据此认定原命令已稳定通过。尚未开展公网模型回答质量评测与 Docker 构建/运行实测。CI 覆盖 Ubuntu/Windows、Python 3.11/3.12，远端结果以实际 workflow 运行为准。

`src/paicli/` 包含 CLI、Agent、工具、存储、检索、计划调度和 API 适配；`tests/` 使用标准库测试框架；`examples/` 提供可查看的源码样例与脚本回复。Dockerfile 供后续容器验证使用。

退出码：`0` 命令结束；`1` 本地 I/O、数据库错误、诊断检查失败，或显式回答评测未执行/含失败；`2` 配置、策略、模型、平台或计划错误；`3` 执行预算耗尽；`130` 用户中断。工具级结果需要单独查看。

## 来源与许可

设计参考了既有 [Java FlowTrail CLI](https://github.com/cloudwallker/flowtrail-cli) 中研究和实践的 PaiCLI 思路。本仓库 Python 源码使用 AI 编程辅助编写，是聚焦核心能力的独立实现，并非逐行翻译。参考版本及署名说明见 [NOTICE](NOTICE)。沿用原有 [MIT 许可证](LICENSE)，保留原贡献者声明。
