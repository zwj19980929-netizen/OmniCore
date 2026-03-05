# OmniCore

面向个人场景的通用 Agent Runtime。

当前版本的定位不是“演示型 AI 原型”，而是一个已经可以长期试运行的个人数字员工底座：它能接收自然语言任务，进行规划、调度工具、后台运行、等待审批、等待事件、复用历史成果，并围绕持续工作上下文推进事情。

## 当前定位

OmniCore 现在已经具备这些核心能力：

- `Tool-First` 调度：任务优先按 `tool_name` 规划和执行，而不是死绑固定 Worker。
- 长期运行：支持 `Session / Job / Artifact`、队列、后台 worker、计划任务、checkpoint、恢复。
- 工作闭环：支持 `Goal / Project / Todo`、独立 `Artifact Store`、成功路径复用。
- 真实工作接入：支持 `api.call`、审批态、目录监听、工作模板、通知。
- 运维与工作台：支持 `Session History`、`Workbench`、`Daily Dashboard`。

一句话概括：

**这是一个面向你自己长期使用的通用 Agent Runtime，而不是一个继续无限扩功能的实验项目。**

## 快速开始

### 1. 安装依赖

```bash
python -m venv venv

# Windows
venv\Scripts\activate

# Linux / macOS
# source venv/bin/activate

pip install -r requirements.txt
playwright install chromium
```

### 2. 配置环境变量

```bash
# Windows PowerShell
Copy-Item .env.example .env

# Linux / macOS
# cp .env.example .env
```

至少补上你要使用的模型 API Key，例如：

- `OPENAI_API_KEY`
- `DEEPSEEK_API_KEY`
- `ANTHROPIC_API_KEY`
- `GEMINI_API_KEY`
- `MINIMAX_API_KEY`

如果你需要代理，建议只通过项目自己的代理环境变量配置，而不是依赖外部 shell 的全局代理：

- `ALLOW_SYSTEM_PROXY=false`（默认）
- `OMNICORE_HTTP_PROXY=...`
- `OMNICORE_HTTPS_PROXY=...`
- `OMNICORE_ALL_PROXY=...`
- `OMNICORE_NO_PROXY=localhost,127.0.0.1,::1`

这样可以避免外部环境里残留的无效代理把模型请求全部打挂。

### 3. 运行方式

```bash
# 交互式 CLI
python main.py

# 直接执行单条任务
python main.py "去 Hacker News 抓取前 10 条新闻并整理成报告"

# 启动主 UI
streamlit run ui/app.py

# 单独启动前台队列 worker
python main.py worker

# 单独启动进程模式队列 worker
python main.py worker --process-loop
```

## 你现在能做什么

### 任务执行

你可以直接用自然语言交代任务，例如：

- `抓取今天的安全漏洞信息并输出为表格`
- `整理这个目录里的新文件并生成摘要`
- `比较几个网页上的同类信息并输出报告`
- `调用某个 HTTP API 获取数据，然后落盘`

### 持续工作

你也可以把任务挂到持续上下文里运行：

- `Goal`：长期目标
- `Project`：某个工作主题
- `Todo`：待推进事项

这意味着多个 Job 不再是互相孤立的，而是可以围绕同一个事项持续推进。

### 自动化与协作

当前版本已经支持：

- 计划任务：`once / interval / daily`
- 目录监听：检测新文件并自动创建任务
- 工作模板：把成功流程保存成可复用模板
- 审批态：高风险动作先进入 `waiting_for_approval`
- 等待态：支持 `waiting_for_event` 和 `blocked`

## 当前架构（简版）

系统当前可以理解为 7 层：

1. 交互层  
CLI、主聊天页、Session History、Workbench、Daily Dashboard。

2. 运行时与编排层  
负责任务提交、队列、worker、checkpoint、任务生命周期。

3. 规划与决策层  
Router、Task Planner、Validator、Critic、Policy Engine。

4. 工具调度层  
Tool Protocol、Tool Registry、Tool Adapters、Task Executor。

5. 工具能力层  
Web、Browser、File、System、API，以及插件工具。

6. 状态与工作上下文层  
Session / Job / Artifact、Goal / Project / Todo、Artifact Store、Work Context。

7. 自动化与协作层  
计划任务、目录监听、模板、审批态、通知。

## 主要目录

```text
OmniCore/
├─ config/         配置
├─ core/           运行时、规划、调度、状态、工具协议
├─ agents/         具体执行能力
├─ memory/         记忆相关能力
├─ utils/          存储、自动化、日志、浏览器等基础设施
├─ ui/             Streamlit 页面
├─ tests/          测试
├─ docs/           架构、状态、收尾说明
└─ main.py         CLI / worker 入口
```

## 常用页面

- `ui/app.py`  
主聊天入口，适合直接交代任务。

- `ui/pages/2_Session_History.py`  
看 Session / Job / Artifact、队列、worker 状态、通知、审批、checkpoint。

- `ui/pages/3_Workbench.py`  
管理 Goal / Project / Todo、模板、目录监听、工作资源。

- `ui/pages/4_Daily_Dashboard.py`  
看今日完成、待审批、等待事件、阻塞事项和推荐下一步。

## 测试

当前建议直接跑全量测试：

```bash
pytest tests -q
```

提交前建议再跑一遍编码健康检查：

```bash
python -m utils.encoding_health
```

当前仓库已收敛到统一测试缓存目录。若出现 `PytestCacheWarning`，通常只和缓存写权限有关，不影响功能判断。

## 文档入口

当前请优先看下面这几份文档：

- [当前架构说明](docs/2026-03-04-通用Agent当前架构说明.md)
- [当前版本收尾说明](docs/2026-03-04-通用Agent当前版本收尾说明.md)
- [文档索引](docs/2026-03-04-通用Agent文档索引.md)
- [路线图状态](docs/2026-03-04-通用Agent路线图状态.md)

早期的路线图和阶段落地说明仍保留在 `docs/` 中，但现在只作为历史参考，不再代表当前系统全貌。

## 当前阶段建议

当前主功能阶段已经完成。

这意味着后续的正确节奏不是继续无上限开新功能，而是：

1. 用真实工作去跑
2. 观察真实卡点
3. 只修稳定性、恢复性、默认行为和交互问题
4. 把它收成一个你自己长期敢用的版本

## License

按仓库实际授权策略处理；如需补充，请在后续单独加入明确许可证文件。
