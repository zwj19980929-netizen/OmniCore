# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

OmniCore is a personal-use general-purpose Agent Runtime — a personal digital assistant backbone that accepts natural language tasks, plans them, dispatches tools, runs in background, supports human-in-the-loop approvals, and persists outcomes across sessions. It is **not** a demo or multi-tenant platform.

## Commands

**Setup:**
```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
playwright install chromium
cp .env.example .env  # then fill in API keys
```

**Run:**
```bash
python main.py                        # Interactive CLI
python main.py "task description"     # One-shot task
python main.py worker                 # Foreground queue worker
python main.py worker --process-loop  # Long-running worker
streamlit run ui/app.py               # Streamlit UI
```

**Test:**
```bash
pytest tests -q                              # Full suite
pytest tests/test_router_unit.py -q          # Single file
python -m utils.encoding_health              # Encoding health check
```

Test files follow the naming convention `test_<feature>_unit.py` (unit) or `test_<feature>_integration.py` (integration). pytest config is in `pytest.ini`; test artifacts go to `data/test-runtime/`.

## Architecture (7 Layers)

| Layer | Responsibility | Key Files |
|---|---|---|
| 1. Interaction | CLI, Streamlit UI, session history, daily dashboard | `main.py`, `ui/app.py`, `ui/pages/` |
| 2. Runtime & Orchestration | Job queue, worker, checkpoint, DAG execution graph | `core/runtime.py`, `core/graph.py`, `core/graph_nodes.py`, `core/graph_conditions.py` |
| 3. Planning & Decision | Intent routing, task planning, validation, policy, replanning | `core/router.py`, `core/task_planner.py`, `core/policy_engine.py`, `core/replanner.py`, `agents/validator.py`, `agents/critic.py` |
| 4. Tool Dispatch | Tool-first scheduling, registry, adapters, pipeline, executor | `core/tool_registry.py`, `core/tool_adapters.py`, `core/task_executor.py`, `core/tool_protocol.py`, `core/tool_pipeline.py` |
| 5. Tool Capability | Web, browser (3-layer), file, system, terminal, MCP | `agents/web_worker.py`, `agents/browser_agent.py`, `agents/file_worker.py`, `agents/system_worker.py`, `agents/terminal_worker.py`, `core/mcp_client.py` |
| 6. State, Memory & Context | Session/Job/Artifact, Goal/Project/Todo, vector memory, session memory, context budget | `core/state.py`, `core/session_memory.py`, `utils/context_budget.py`, `utils/runtime_state_store.py`, `utils/artifact_store.py`, `utils/work_context_store.py`, `memory/` |
| 7. Automation & Events | Scheduled tasks, event sources (web watch, webhook, email), templates, approvals | `utils/workflow_automation_store.py`, `utils/event_sources/`, `utils/event_dispatcher.py` |

## Core Data Model

- **Session** — persistent work session aggregating multiple Jobs
- **Job** — single task submission (`queued` → executing → done/waiting/blocked)
- **Task** — sub-step inside a Job, bound to a specific tool
- **Artifact** — reusable output (files, downloads, structured results)
- **Goal / Project / Todo** — continuous work context threading multiple Jobs
- **PolicyDecision** — approval/risk gate records

## Key Execution Flows

1. **Normal:** User input → `submit_job` → Router/Planner → Tool Dispatch → Validator/Critic → Artifact → persist
2. **Background:** Queue → Worker → `run_next_queued_task` → write back Job/Artifact/notification
3. **Approval gate:** High-risk action → `waiting_for_approval` → user approve/reject → resume or block
4. **Schedule/Watch:** Cron trigger or file-system event → auto-create Job → worker consume

## LLM Routing

Multi-provider via LiteLLM. Supported providers: OpenAI, Anthropic, Gemini, DeepSeek, MiniMax, Kimi, Zhipu. Model capability and pricing metadata are in `config/models.yaml` and `config/model_pricing.yaml`. Active provider is controlled by `DEFAULT_MODEL` and `PREFERRED_PROVIDER` env vars. Cost-aware routing available via `core/complexity_scorer.py` and `utils/cost_tracker.py`.

LLM prompt templates are `.txt` files under `prompts/`. Prompt section registry (`core/prompt_registry.py`) supports section-level caching and token budgeting.

## Key Config

| File | Purpose |
|---|---|
| `.env` (from `.env.example`) | API keys, proxy, model routing, feature flags |
| `config/models.yaml` | Model capability overrides and provider API base URLs |
| `config/model_pricing.yaml` | Model pricing metadata |
| `config/agents.yaml` | Agent type definitions and registry |
| `config/settings.py` | Runtime settings module (all tunable parameters) |
| `config/mcp_servers.yaml` | MCP server configurations |
| `prompts/` | LLM prompt templates |

**Notable env vars:**
- `DEFAULT_MODEL`, `PREFERRED_PROVIDER` — model routing
- `REQUIRE_HUMAN_CONFIRM` — approval gate toggle
- `BROWSER_FAST_MODE`, `BLOCK_HEAVY_RESOURCES`, `STATIC_FETCH_ENABLED` — browser tuning
- `OMNICORE_HTTP_PROXY` — project-scoped proxy (system proxy is disabled by default)
- `CHROMA_PERSIST_DIR` — vector memory path
- `VISION_MODEL`, `VISION_PERCEPTION_MODEL` — vision model selection
- `LLM_MAX_TOKENS`, `LLM_ROUTER_MAX_TOKENS` — token limits
- `BROWSER_STEP_MEMORY_SIZE` / `BROWSER_DEDUP_THRESHOLD` / `BROWSER_RECENT_STEPS_IN_PROMPT` — Browser 自我规划优化 P0 指纹去重
- `BROWSER_PLAN_ENABLED` / `BROWSER_MAX_PLAN_STEPS` / `BROWSER_MAX_REPLANS` / `BROWSER_STEP_STUCK_THRESHOLD` — P1 任务级 Plan
- `BROWSER_UNIFIED_ACT_ENABLED` — P2 单 Prompt 决策开关（默认关闭，稳定后切换）
- `BROWSER_PLAN_MEMORY_ENABLED` — P3 跨会话长期 Plan 记忆主开关（B1 / B6 复用）
- `BROWSER_STRATEGY_REFACTOR_ENABLED` — B6 三模式策略链（LoginReplay → Batch/Unified → Legacy），默认关闭
- `BROWSER_BATCH_EXECUTE_ENABLED` / `BROWSER_SEQUENCE_MODEL` / `BROWSER_MAX_SEQUENCE_ACTIONS` / `BROWSER_MAX_CORRECTIONS` — P4 批量执行与按需纠偏
- `BROWSER_DOM_CHECKPOINT_ENABLED` / `BROWSER_VISUAL_VERIFY_ENABLED` / `BROWSER_CORRECTION_ESCALATE_TO_REASONING` — P4 检查点与验证配置
- `BROWSER_SITE_KNOWLEDGE_DB` / `BROWSER_SELECTOR_HINT_TOP_K` / `BROWSER_SELECTOR_MIN_SUCCESS_RATE` / `BROWSER_SELECTOR_DECAY_DAYS` / `BROWSER_SITE_HINTS_INJECT` / `BROWSER_SITE_HINTS_EXEC_INJECT` / `BROWSER_LOGIN_REPLAY_ENABLED` — B1 站点选择器 / 登录流 / 决策注入 / 执行层 fallback 注入（主开关复用 `BROWSER_PLAN_MEMORY_ENABLED`）
- `BROWSER_STRATEGY_LEARNING_ENABLED` / `BROWSER_STRATEGY_DB` / `BROWSER_STRATEGY_MIN_SAMPLES` / `BROWSER_STRATEGY_SKIP_THRESHOLD` — B5 失败策略自适应学习（per-(domain, role) 成功率驱动 fallback 重排 + skip）
- `ANTI_BOT_PROFILE_ENABLED` / `ANTI_BOT_PROFILE_DB` / `ANTI_BOT_INITIAL_DELAY_SEC` / `ANTI_BOT_MAX_DELAY_SEC` / `ANTI_BOT_UA_POOL_FILE` / `ANTI_BOT_BLOCK_DECAY_DAYS` / `ANTI_BOT_SUCCESS_TO_COOLDOWN` — B2 反爬 domain 画像与 UA 池
- `BROWSER_VISION_CACHE_ENABLED` / `BROWSER_VISION_CACHE_DB` / `BROWSER_VISION_CACHE_TTL_DAYS` / `BROWSER_VISION_CACHE_BYPASS_KEYWORDS` — B3 视觉描述缓存（按 page fingerprint 复用同模板页面的视觉描述）
- `BROWSER_IFRAME_ENABLED` / `BROWSER_TAB_MANAGEMENT_ENABLED` / `BROWSER_IFRAME_AUTO_SCAN_ON_STUCK` / `BROWSER_MAX_TAB_COUNT` — B4 iframe / 多 tab 感知层暴露 + 执行层自动扫描兜底 + tab 回收

## 架构演进记录

| 阶段 | 完成日期 | 关键内容 |
|---|---|---|
| S1–S6 | 2026-03 | 基础 Runtime、Tool Dispatch、Coordinator/Subagent、Fail-Closed 安全分层（见 `docs/archive/` 历史记录） |
| P2-2 成本感知路由 | 2026-03-31 | `core/complexity_scorer.py`、`utils/cost_tracker.py`、`/cost` 命令 |
| Browser 自我规划优化 P0+P2+P1 | 2026-04-14 | 指纹去重 + 单 Prompt + 任务级 Plan（详见 `docs/design/2026-04-14-browser-planning-optimization.md`） |
| Browser 批量执行 P4 | 2026-04-15 | 一次规划批量执行 + DOM 检查点 + 视觉纠偏（详见 `docs/design/2026-04-15-browser-batch-execute-optimization.md`） |
| 记忆能力优化 A 组 | 2026-04-16 | A1 衰减+TTL+归档 / A2 实体倒排索引 + Router 注入 + `delete_by_entity` / A3 Skill 前置注入 / A4 三层记忆 + session-close purge 钩子 / A5 偏好学习(规则+LLM 层)+ Router 注入（详见 `docs/design/2026-04-16-memory-and-browser-optimization.md`） |
| 网页操作优化 B 组(数据层) | 2026-04-16 | B1 `utils/site_knowledge_store.py` 站点选择器 + 登录流 + 动作模板 SQLite 存储，`browser_decision._build_site_hints_block` 注入 LLM prompt，`record_action` 自动写回；B2 `utils/anti_bot_profile.py` domain 画像 + 指数退避 + UA 池，`web_worker._record_anti_bot_block` 三处阻断反馈（详见 `docs/design/2026-04-16-memory-and-browser-optimization.md` §B1/B2） |
| Browser 视觉缓存 B3 | 2026-04-17 | `utils/page_fingerprint.py`（域名+归一化路径+DOM 结构签名）+ `utils/vision_cache.py`（SQLite，TTL 默认 7 天 + 高风险关键词 bypass），`browser_perception.observe()` 视觉调用前查缓存命中即跳过 vision LLM；`BrowserAgent.run()` 把 task 透传到感知层用于 bypass 判定（详见 `docs/design/2026-04-16-memory-and-browser-optimization.md` §B3） |
| B1 执行层 + B5 失败策略自适应学习 | 2026-04-17 | 新增 `utils/strategy_stats.py`（SQLite per-(domain, role, strategy) 成功率/延迟统计）；改造 `agents/browser_execution.py` 的 `try_click/input_with_fallbacks`：site_hint 前置（B1）+ ranked/skip 重排（B5）+ 每次尝试埋点回写 strategy_stats 和 site_knowledge_store；双开关 `BROWSER_PLAN_MEMORY_ENABLED` / `BROWSER_STRATEGY_LEARNING_ENABLED` 默认 off 零开销（详见 `docs/design/2026-04-16-memory-and-browser-optimization.md` §B1.7/§B5.7） |
| B2 接入 + B4 iframe/多 tab | 2026-04-17 | `utils/browser_toolkit.py` 加 `apply_throttle_hint` / response listener(429→rate_limit, 503→service_unavailable) / `goto()` 成功埋点 `record_request(True)` / `_enforce_tab_cap()` 超限关老 tab；`agents/browser_agent._initialize_session` 在 create_page 前调 `suggest_throttle` 覆盖 UA+flip headless；感知层 `observe()` 把 `list_frames`/`list_tabs` 挂到 snapshot，`browser_decision` 两个 prompt 注入 `{available_frames}` / `{available_tabs}` 并扩展 `switch_iframe/switch_tab/close_tab` 合法动作；执行层 `try_click/input_with_fallbacks` 主 frame 失败后可选择自动扫 iframe 兜底(详见 `docs/design/2026-04-16-memory-and-browser-optimization.md` §B2.7/§B4) |
| B1 login_replay + record_template + B6 三模式解耦 | 2026-04-18 | 新增 `agents/browser_login_replay.py`（`try_replay_login` 按 `get_login_flow` 逐步 replay + `dom_checkpoint` 校验 + 成功/失败自动 `record_login_flow`）；新增 `utils/browser_template_recorder.py` `record_template_from_run`（search/navigate/form 意图尾部抓取 `record_template`）；新增 `agents/browser_strategies/` 包（`DecisionStrategy` 抽象 + `LegacyPerStepStrategy` / `UnifiedActStrategy` / `BatchExecuteStrategy` / `LoginReplayStrategy`）与 `StrategyPicker.build_chain`（LoginReplay → Batch/Unified → Legacy）；新增 `agents/page_assessment_cache.py` 并在 `BrowserAgent.__init__` 挂 per-run 实例 + `run()` 开头 `clear()` + `get_or_compute_assessment` 暴露给策略；`prompts/browser_act.txt` / `prompts/browser_action_decision.txt` 把 `site_hints` 从 append-after 迁到 `{site_hints}` 模板 kwarg；`browser_agent.py` 提取 `_run_per_step_loop` + 新增 `_run_with_strategies` 入口，`BROWSER_STRATEGY_REFACTOR_ENABLED=true` 时走策略链，默认 off 零影响；新增测试 `test_browser_login_replay_unit` 16 条 / `test_browser_template_recorder_unit` 23 条 / `test_strategy_picker_unit` 24 条，回归 179 条浏览器单测 + 全量 1110 条全绿 |

### Browser 规划优化落地情况（2026-04-14）

P0 指纹去重（已上线）
- `agents/browser_decision.py`：新增 `_step_fingerprints` OrderedDict、`_fingerprint_action`、`_is_repeat_action`、`format_repeated_actions_for_llm`；`record_action` 自动写入指纹
- `_sanitize_planned_action` 末尾拦截命中 ≥ `BROWSER_DEDUP_THRESHOLD` 的动作，写 `web_debug_recorder` 事件 `browser_dedup_rejected`
- 三份 Prompt（`browser_unified_plan.txt` / `browser_page_assessment.txt` / `browser_action_decision.txt`）新增 `{repeated_actions}` 区块与"BLACKLISTED"规则
- `_format_recent_steps_for_llm` 默认使用 `BROWSER_RECENT_STEPS_IN_PROMPT`（默认 8）
- 测试 `tests/test_browser_step_dedup_unit.py`（8 用例，全部通过）

P2 三合一 Prompt 收敛（代码已落地，默认开关关闭）
- 新增 `prompts/browser_act.txt`：合并 unified_plan + page_assessment，返回 `{thinking, goal_satisfied, action, confidence, need_replan}`
- 新增 `BrowserDecisionLayer._act_with_llm(...)`：单次 LLM 调用
- `_plan_next_action`：`BROWSER_UNIFIED_ACT_ENABLED=true` 时跳过 `asyncio.gather(unified, assess)` 和 `_decide_action_with_llm`，单步 LLM 调用从 2–4 次降到 1 次；默认保持旧三路径以便回滚
- 旧 Prompt 文件保留（按设计 2 周稳定期后再物理删除）

P1 任务级 Plan（已接入执行循环）
- 新增 `agents/browser_task_plan.py`：`PlanStep` / `TaskPlan` / `build_initial_plan` / `step_advance` / `replan`
- 新增 `prompts/browser_task_plan.txt` 与 `prompts/browser_step_advance.txt`
- `BrowserAgent.run` 入口在 `BROWSER_PLAN_ENABLED=true` 时构造 `TaskPlan` 挂到 `decision._task_plan`
- 每个 `_execute_step` 返回后调用 `step_advance` 推进 / 跳过；连续卡顿 ≥ `BROWSER_STEP_STUCK_THRESHOLD` 或 LLM 明示 `need_replan` 触发 `replan()`；`BROWSER_MAX_REPLANS` 控制最多 replan 次数
- `browser_act.txt` 通过 `{plan_context}` 变量注入当前 step/completed/remaining，LLM 据此选择下一动作
- 测试 `tests/test_browser_task_plan_unit.py`（7 用例，全部通过）

P3 跨会话 Plan 记忆（未实现）
- 仅预留 `BROWSER_PLAN_MEMORY_ENABLED` 开关；等 P0/P1/P2 线上回放稳定后再落地

P4 批量执行与按需纠偏（已落地，默认开关关闭）
- 核心思路：**一次 LLM 规划完整动作序列 → 批量执行不调模型 → DOM 检查点零成本校验 → 视觉模型验证结果 → 偏差时纠偏**
- 解决问题：原架构每步浏览器动作调 2-3 次 LLM（deepseek-reasoner），登录任务 15-20 次调用；批量模式降至 2-3 次
- 新增 `agents/browser_action_sequence.py`：`ActionSequence` / `SequenceAction` / `DomCheckpoint` 数据结构，`generate_action_sequence` / `visual_verify` / `plan_correction` 三个 LLM 入口
- 新增 `utils/dom_checkpoint.py`：6 种检查点类型（`value_change` / `url_change` / `element_appear` / `element_disappear` / `text_appear` / `attribute_change`），纯 DOM 查询零 LLM 成本
- 新增 Prompt：`prompts/browser_action_sequence.txt`（动作序列生成）、`prompts/browser_visual_verify.txt`（视觉验证）、`prompts/browser_correction.txt`（纠偏规划）
- `BrowserAgent.run()`：`BROWSER_BATCH_EXECUTE_ENABLED=true` 时进入 `_run_batch_mode`，生成序列失败自动回退到逐步模式
- 分层模型：`BROWSER_SEQUENCE_MODEL` 可指定快模型做序列生成，`BROWSER_CORRECTION_ESCALATE_TO_REASONING=true` 时 major 偏差升级到推理模型
- 纠偏流程：视觉验证返回 `deviation`（`none` / `minor` / `major`），`minor` 用快模型局部调整，`major` 用推理模型重规划，最多 `BROWSER_MAX_CORRECTIONS` 次
- 与 P0/P1/P2 兼容：指纹去重在批量模式下仍生效；TaskPlan 仍保留为宏观指引；`BROWSER_BATCH_EXECUTE_ENABLED` 优先级高于 `BROWSER_UNIFIED_ACT_ENABLED`
- 测试 `tests/test_browser_batch_execute_unit.py`（28 用例，全部通过）

## Coding Conventions

- Python: 4-space indent, `snake_case` for functions/variables, `CamelCase` for classes, UTF-8 encoding throughout
- Commit style: `feat:` / `fix:` / `refactor:` prefixes, short imperative subject lines
- Never commit `.env` or the `data/` directory

## No-Hardcoding Policy

**Hardcoding is prohibited unless absolutely necessary.** The following must be managed via config files or environment variables — never written directly in Python source code:

| Category | Correct | Wrong |
|---|---|---|
| **Model names** | Read from `config/models.yaml` or env vars | `model="gpt-4o"` in code |
| **API keys / URLs** | Read from `.env` env vars | String literals in code |
| **Token limits / timeouts** | Define in `config/settings.py`, override via env vars | `max_tokens=4096` scattered in business logic |
| **Retry counts / thresholds** | Centralize in `config/settings.py` | Ad-hoc magic numbers everywhere |
| **Prompt templates** | Place in `prompts/*.txt`, load via `prompt_registry` | Multi-line strings in Python files |
| **Agent / Tool definitions** | Register in `config/agents.yaml` / `tool_registry` | `if tool_name == "xxx"` hardcoded branches |

**Permitted exceptions:**
- Pure algorithmic constants (e.g. coefficients in mathematical formulas)
- Class-level scoring weight constants explicitly labeled (e.g. `_SCORE_WEIGHTS`)
- Test data in unit tests

**When hardcoding is found:** Extract the value to `config/settings.py` (with `os.getenv` for env var override), or move it into the appropriate YAML config file.

## Documentation Conventions

All docs must be placed in `docs/` subdirectories — **never directly in the `docs/` root**. See [docs/README.md](docs/README.md) for full conventions.

| Directory | Purpose |
|---|---|
| `docs/architecture/` | Core architecture reference (currently active) |
| `docs/design/` | Feature design proposals and optimization plans |
| `docs/archive/` | Historical docs, fix reports, archived content |

**Naming format:** `YYYY-MM-DD-<short-title>.md`

**Required document header:**
```markdown
# Title

> Status: draft | in-progress | completed | archived
> Created: YYYY-MM-DD
> Last updated: YYYY-MM-DD
```

**Key rules:**
- New proposals go in `design/`; mark status when done; move to `archive/` when outdated
- Do not write changelog-style "completion summaries" — commit messages and PR descriptions are sufficient
- Prefer updating existing docs over creating new ones
- Do not hardcode config values in docs — reference config files or env vars instead
- The authoritative architecture reference is `docs/architecture/2026-03-04-通用Agent当前架构说明.md`
