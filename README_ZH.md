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
| 工具 | `read_file`、`list_files`、`search_code`、`write_file`；参数类型及工作区路径校验 |
| 写入审批 | 展示完整 diff，以相对路径、原内容和新内容计算 SHA-256 指纹；每次写入单独精确审批 |
| 会话 | SQLite 保存 system/user/assistant/tool 消息；支持会话 ID 续接及旧轮次的确定性摘录 |
| 代码检索 | Python AST 与导入/常量/顶层代码、JSX/MJS/CSS/HTML，限制行窗口，FTS5/BM25 排名、源码位置和过期结果过滤 |
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

**2026-10-23** 本地验证环境为 Windows / Python 3.12.14：**64 项测试通过，0 失败、0 跳过**。覆盖源码行号与模块/JSX 检索、过期索引、平台响应结构、SSE 游标/完整性/限额/截止时间与认证错误控制帧、真实本地 HTTP 与 CLI 子进程、路径及链接边界、写入审批、Agent 上下文、DAG 并发取消及 cp1252/ASCII 子进程输出。真实 FlowTrail 联调脚本也已通过。尚未验证真实远程模型效果和 Docker 构建。CI 覆盖 Ubuntu/Windows、Python 3.11/3.12，远端结果以实际 workflow 运行为准。

`src/paicli/` 包含 CLI、Agent、工具、存储、检索、计划调度和 API 适配；`tests/` 使用标准库测试框架；`examples/` 提供可查看的源码样例与脚本回复。Dockerfile 供后续容器验证使用。

退出码：`0` 命令结束；`1` 本地 I/O、数据库错误或诊断检查失败；`2` 配置、策略、模型、平台或计划错误；`3` 执行预算耗尽；`130` 用户中断。工具级结果需要单独查看。

## 来源与许可

设计参考了既有 [Java FlowTrail CLI](https://github.com/cloudwallker/flowtrail-cli) 中研究和实践的 PaiCLI 思路。本仓库 Python 源码使用 AI 编程辅助编写，是聚焦核心能力的独立实现，并非逐行翻译。参考版本及署名说明见 [NOTICE](NOTICE)。沿用原有 [MIT 许可证](LICENSE)，保留原贡献者声明。
