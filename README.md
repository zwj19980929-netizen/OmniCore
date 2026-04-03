# OmniCore

面向个人场景的通用 Agent Runtime。

OmniCore 不是"演示型 AI 原型"，而是一个可长期运行的个人数字员工底座：接收自然语言任务，进行规划、调度工具、后台运行、等待审批、复用历史成果，并围绕持续工作上下文推进事情。

## 核心能力

- **Tool-First 调度** — 任务按 `tool_name` 规划和执行，不绑定固定 Worker
- **多模型路由** — 通过 LiteLLM 支持 OpenAI / Anthropic / Gemini / DeepSeek / MiniMax / Kimi，按任务复杂度和成本智能选模型
- **长期运行** — Session / Job / Artifact、队列、后台 Worker、计划任务、Checkpoint 与恢复
- **工作闭环** — Goal / Project / Todo 持续上下文、Artifact Store、成功路径复用
- **浏览器自动化** — 三层架构（感知 → 决策 → 执行），支持视觉模型、iframe/Shadow DOM、反检测
- **知识与记忆** — Chroma 向量库 RAG、Skill Library 经验复用、Session Memory 提炼
- **安全与审批** — Policy Engine 风险分级、Human-in-the-Loop 审批态、MCP 信任分层
- **事件驱动** — 网页变更监听、Webhook、邮件事件源，自动创建任务
- **运维仪表盘** — Session History、Workbench、Daily Dashboard、Runtime Metrics

## 快速开始

### 1. 安装依赖

```bash
python -m venv venv
source venv/bin/activate     # macOS / Linux
# venv\Scripts\activate      # Windows

pip install -r requirements.txt
playwright install chromium
```

### 2. 配置环境变量

```bash
cp .env.example .env
```

至少填入你要使用的模型 API Key（`OPENAI_API_KEY` / `DEEPSEEK_API_KEY` / `ANTHROPIC_API_KEY` 等）。

代理配置建议使用项目级环境变量，而非全局 shell 代理：

```
ALLOW_SYSTEM_PROXY=false          # 默认禁用系统代理
OMNICORE_HTTP_PROXY=...           # 项目级代理
```

### 3. 运行

```bash
python main.py                        # 交互式 CLI
python main.py "任务描述"              # 单次执行
python main.py worker                 # 前台队列 Worker
python main.py worker --process-loop  # 长期运行 Worker
streamlit run ui/app.py               # Streamlit UI
```

### 4. 测试

```bash
pytest tests -q                       # 全量测试
pytest tests/test_router_unit.py -q   # 单文件
python -m utils.encoding_health       # 编码健康检查
```

## 架构概览（7 层）

| 层 | 职责 | 关键模块 |
|---|---|---|
| 1. 交互层 | CLI、Streamlit UI、Dashboard | `main.py`, `ui/` |
| 2. 运行时与编排 | 任务队列、Worker、Checkpoint、DAG 执行图 | `core/runtime.py`, `core/graph.py` |
| 3. 规划与决策 | 意图路由、任务规划、验证、风险策略 | `core/router.py`, `core/task_planner.py`, `core/policy_engine.py` |
| 4. 工具调度 | Tool Protocol、Registry、Adapters、Pipeline | `core/tool_registry.py`, `core/tool_pipeline.py`, `core/task_executor.py` |
| 5. 工具能力 | Web、Browser、File、System、Terminal、MCP | `agents/` |
| 6. 状态与记忆 | Session/Job/Artifact、Goal/Project/Todo、向量记忆 | `core/state.py`, `memory/`, `utils/*_store.py` |
| 7. 自动化与协作 | 计划任务、事件监听、模板、审批、通知 | `utils/workflow_automation_store.py`, `utils/event_sources/` |

## 项目结构

```
OmniCore/
├── config/         配置（settings.py, models.yaml, agents.yaml）
├── core/           运行时、规划、调度、状态、工具协议（~40 模块）
├── agents/         执行能力（Browser、Web、File、System、Terminal）
├── memory/         向量记忆与知识库（Chroma、Skill Store）
├── utils/          基础设施（存储、日志、浏览器工具、事件源）
├── prompts/        LLM Prompt 模板（.txt）
├── templates/      Jinja2 报告模板
├── ui/             Streamlit 页面（主页 + 4 个子页面）
├── tests/          测试（unit / integration）
├── docs/           文档（architecture / design / archive）
└── main.py         CLI / Worker 入口
```

## 文档

文档按类型组织在 `docs/` 子目录中，详见 [docs/README.md](docs/README.md)：

- **[核心架构说明](docs/architecture/2026-03-04-通用Agent当前架构说明.md)** — 权威架构参考
- **[设计方案](docs/design/)** — 功能设计与规划
- **[历史归档](docs/archive/)** — 已完成的阶段文档与修复报告

## 关键配置

| 文件 | 用途 |
|---|---|
| `.env` | API Key、代理、模型路由、功能开关 |
| `config/models.yaml` | 模型能力与定价元数据 |
| `config/agents.yaml` | Agent 类型注册 |
| `config/settings.py` | 运行时配置（所有可调参数集中管理） |
| `prompts/` | LLM Prompt 模板 |

## License

按仓库实际授权策略处理；如需补充，请单独加入许可证文件。
