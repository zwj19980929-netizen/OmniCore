# Repository Guidelines

## Project Structure & Module Organization
`main.py` is the CLI and worker entrypoint. Core orchestration lives in `core/` (runtime, routing, planning, tool protocol), execution agents in `agents/`, shared infrastructure in `utils/`, config in `config/`, persistent memory in `memory/`, Streamlit UI in `ui/` and `ui/pages/`, prompts in `prompts/`, and regression/unit tests in `tests/`. Keep design notes in `docs/`; treat `data/`, `outputs/`, and runtime caches as generated state and do not commit them.

## Build, Test, and Development Commands
```bash
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
```
Use `python main.py` for the interactive CLI, `python main.py "task"` for one-off runs, `python main.py worker` or `python main.py worker --process-loop` for queue workers, and `streamlit run ui/app.py` for the UI. Run `pytest tests -q` before submitting changes. For focused work, use `pytest tests/test_router_unit.py -q`. Run `python -m utils.encoding_health` before review.

## Coding Style & Naming Conventions
Follow existing Python style: 4-space indentation, `snake_case` for functions, modules, and tests, `CamelCase` for classes, and concise docstrings only when behavior is non-obvious. Keep modules aligned to the current layer boundaries: orchestration in `core/`, tool-specific behavior in `agents/`, shared helpers in `utils/`. Prefer UTF-8 text files and descriptive prompt/config names such as `browser_page_assessment.txt`.

**禁止使用 `print()`**：代码中不允许使用 `print()` 输出信息。所有日志输出必须使用项目的日志系统（`utils/logger.py` 中的 `log_agent_action`、`log_error`、`log_warning`、`log_debug_metrics` 等，或 `utils/structured_logger.py` 中的 `get_structured_logger()`）。

## Testing Guidelines
Pytest is configured in `pytest.ini` with `tests/` as the main test root and temp output under `data/test-runtime/`. Add unit tests as `tests/test_<feature>_unit.py` when changing isolated behavior, and extend existing integration-style tests when touching browser/runtime flows. Cover both success paths and guardrail or failure paths.

## Commit & Pull Request Guidelines
Recent history uses short imperative subjects, often with `feat:` or `fix:` prefixes, plus plain summaries like `Improve web agent search and login perception`. Match that style and keep the subject focused on one behavior change. Pull requests should explain the user-visible impact, list the verification commands you ran, note any `.env`, Playwright, or model-provider requirements, and include screenshots for `ui/` changes.

## Security & Configuration Tips
Start from `.env.example`; never commit `.env`, API keys, or generated `data/` contents. If a change alters model routing, browser automation, or approval behavior, document the new environment variables or operational risks in `README.md` or `docs/`.

## Architecture (7-Direction Upgrade — Completed 2026-03-20)
All 7 directions of the architecture upgrade are now integrated into the runtime. See `docs/2026-03-19-架构升级集成计划.md` for the original plan and file index.

| Direction | Status | Key Integration Points |
|-----------|--------|----------------------|
| 1. Composable Graph | ✅ Done | `after_validator` and other edge functions consult `StageRegistry.build_execution_plan()` to respect `skip_condition` |
| 2. Agent Registry | ✅ Done | Router prompt auto-injects agent descriptions from `config/agents.yaml` via `{{AGENT_CAPABILITIES}}`; `WorkerPool` uses factory pattern backed by registry |
| 3. Browser 3-Layer Split | ✅ Done | `agents/browser_perception.py`, `browser_decision.py`, `browser_execution.py` |
| 4. Message Bus | ✅ Done | 6 key `shared_memory` keys dual-written to `MessageBus`; reads prefer bus with shared_memory fallback |
| 5. Persistence Coordinator | ✅ Done | `_finalize_runtime_result()` calls `PersistenceCoordinator.complete_job()` for unified 3-store write |
| 6. Structured Logging | ✅ Done | All 7 stage nodes, LLM calls, browser actions, and job lifecycle emit JSONL to `data/logs/omnicore.log` |
| 7. Adaptive Routing | ✅ Done | `_after_parallel_executor_adaptive` in `core/graph.py` |

**Key conventions introduced by the upgrade:**
- **Dual-write pattern**: new code writes to both `MessageBus` and `shared_memory`; reads go bus-first. Do not remove `shared_memory` writes until all consumers are migrated.
- **`config/agents.yaml`**: add new agent types here instead of modifying code. The router prompt and `WorkerPool` pick them up automatically.
- **`@register_stage` decorator**: all graph nodes use this; stage metadata drives `build_execution_plan()`.
- **JSONL structured logs**: use `get_structured_logger()` + `LogContext` for any new instrumentation. Logs rotate daily in `data/logs/`.

**Known test state**: 304 passed, 24 failed (pre-existing failures in `test_web_worker_unit.py` and `test_web_worker_perception.py`, unrelated to the upgrade).

## 能力拓展方向（未实施）

详细方案见 [`docs/2026-03-28-能力拓展方向规划.md`](docs/2026-03-28-能力拓展方向规划.md)，共 8 个方向：

| 优先级 | 方向 | 状态 |
|--------|------|------|
| P0 | MCP 工具生态接入 | ⬜ 未开始 |
| P0 | Skill Library 经验复用 | ⬜ 未开始 |
| P1 | IM Bot 远程访问 | ⬜ 未开始 |
| P1 | 知识库 RAG 深度集成 | ⬜ 未开始 |
| P2 | 多模态输入/输出 | ✅ 已完成 (2026-03-28) |
| P2 | 成本感知智能路由 | ⬜ 未开始 |
| P3 | 事件驱动信息流 | ⬜ 未开始 |
| P3 | 多 Agent 协作 | ⬜ 未开始 |

## Current Work In Progress

### 已完成: 天气硬编码清理 (2026-03-24)
移除了所有天气领域硬编码（router 确定性路由、web_worker 天气特殊路径、critic 天气验证），通用智能体不做领域特化。天气查询现统一走 LLM 路由。

### 已完成: LiteLLM 模型名修复 (2026-03-24)
- `core/llm.py`: OpenAI 原生模型不再加 `openai/` 前缀（litellm 直接认识 `gpt-*` 系列），修复了 `Provider List` 报错导致视觉模型调用失败的问题
- 新增 `litellm.suppress_debug_info = True` 抑制未知模型的调试噪音

### 已完成: 视觉模型优化 (2026-03-24)
详细计划见 `docs/视觉模型优化计划.md`。共 4 个优化项，全部已完成：

| 优化项 | 优先级 | 状态 |
|--------|--------|------|
| 新页面自动视觉感知 | P0 | ✅ 已完成 |
| 操作后视觉验证 | P0 | ✅ 已完成 |
| WAIT 视觉变化检测 | P1 | ✅ 已完成 |
| 连续截图进度感知 | P2 | ✅ 已完成 |

### 已完成: 代码架构重构 + 网页操作健壮性优化 (2026-03-27, 加固 2026-03-28)

两份优化方案已全部实施并加固，覆盖 9 个已识别的技术债务项：

| 文档 | 覆盖范围 | 优先级分布 |
|------|---------|-----------|
| [`docs/2026-03-26-代码架构重构方案.md`](docs/2026-03-26-代码架构重构方案.md) | graph.py 职责过载、llm.py 重复代码、browser_agent.py 内联 JS、BrowserAgent.run 拆分、router.py 魔法数字 | P0×2, P1×2, P2×1 |
| [`docs/2026-03-26-网页操作健壮性优化方案.md`](docs/2026-03-26-网页操作健壮性优化方案.md) | Vision 预算限流、像素校验误报、Shadow DOM/iframe 穿透、搜索选择器韧性 | P0×2, P1×1, P2×1 |

问题分析来源：[`问题归类.md`](问题归类.md)

#### 实施进度（代码架构重构）

| 任务 | 状态 | 说明 |
|------|------|------|
| P0: graph.py 删除 legacy 函数 | ✅ 已完成 (2026-03-27) | 删除 `_legacy_synthesize_user_facing_answer`、`_legacy_finalize_node`、`_legacy_finalize_node_v2`，共 ~285 行；2345→2060 行 |
| P0: llm.py 重复代码消除 | ✅ 已完成 (2026-03-27) | 提取 `_build_chat_kwargs` + `_build_llm_response`；简化 `parse_json_response` 删除 46 行重复外层逻辑；修复 `achat` 缺失的结构化日志和拒绝处理；990→929 行 |
| P1: browser_agent.py 内联 JS 分离 | ✅ 已完成 (2026-03-27) | 提取 2 个大 JS 块（~255行）到 `utils/perception_scripts.py` 的 `SCRIPT_FALLBACK_SEMANTIC_SNAPSHOT` / `SCRIPT_EXTRACT_INTERACTIVE_ELEMENTS`；browser_agent.py 3021→2770 行 |
| P1: BrowserAgent.run 拆分 | ✅ 已完成 (2026-03-27) | 637 行 `run()` 拆为 `_initialize_session` (导航+初始化) + `_execute_step` (单步循环体) + `_build_final_result` (收尾)；`run()` 本身缩至 47 行；browser_agent.py 2770→2856 行（含新方法） |
| P2: router.py 魔法数字提取 | ✅ 已完成 (2026-03-27) | 10 个硬编码权重提取为 `RouterAgent._SCORE_WEIGHTS` 类常量 |

#### 实施进度（网页操作健壮性优化）

| 任务 | 状态 | 说明 |
|------|------|------|
| P0: Vision Fallback 预算限流 | ✅ 已完成 (2026-03-27) | 新增 `VisionBudget` dataclass；`run()` 入口重置预算；`_decide_action_with_vision` / `_vision_check_page_relevance` 调用前检查，超限返回 None；`can_call()` 同时检查调用次数和 token 累计预算；视觉调用强制 30s 超时（`asyncio.wait_for`），超时后记录调用并返回 None；新增配置 `VISION_MAX_CALLS_PER_RUN=5`、`VISION_COOLDOWN_SECONDS=3.0`、`VISION_MAX_TOKENS_PER_RUN=20000`、`VISION_CALL_TIMEOUT=30000` |
| P0: 像素级视觉校验误报优化 | ✅ 已完成 (2026-03-27) | `utils/image_diff.py` 新增 `compute_pixel_diff_roi`（按比例排除顶/底 5% 噪声区域，适配各种 viewport）、`compute_block_diff`（16×16块均值比较）、`screenshots_meaningfully_differ`；`screenshots_differ` 升级为分层策略；像素阈值 0.02→**0.05**，块级阈值通过 `VISION_BLOCK_DIFF_THRESHOLD=0.08` 可配置；`_verify_action_effect` 改用 `screenshots_meaningfully_differ` |
| P1: Shadow DOM / iframe 穿透增强 | ✅ 已完成 (2026-03-27) | Shadow DOM: `querySelectorAllDeep`/`querySelectorDeep` 已注入各 JS 脚本，带 depth=10 递归深度限制防止栈溢出；iframe: `BrowserPerceptionLayer._extract_iframe_elements` 遍历所有可见 frame 并合并到 `observe()` 快照；`switch_to_iframe` 支持 `>>>` 嵌套语法；新增 `list_iframes` 枚举接口 |
| P2: 搜索引擎选择器韧性增强 | ✅ 已完成 (2026-03-27) | `SearchEngineProfile` 含 `fallback_result_selectors`/`last_verified`；`validate_selectors` 使用 `querySelectorAllDeep` 穿透 Shadow DOM 检测选择器；健康检查主动影响行为：命中率=0 且无 fallback 时跳过 CSS 提取直接走视觉兜底；`BrowserExecutionLayer` 新增 `perception` 参数并在 `BrowserAgent` 初始化时注入 |

#### 加固轮次 (2026-03-28)

对网页操作健壮性优化的 4 个实施项进行 code review 后修复了以下遗漏：

| 修复项 | 涉及文件 | 说明 |
|--------|---------|------|
| VisionBudget token 预算未执行 | `agents/browser_agent.py` | `can_call()` 原先只检查调用次数和冷却时间，未检查 `tokens_used >= max_total_tokens`，已补上 |
| 视觉调用缺少独立超时 | `agents/browser_agent.py` | `_decide_action_with_vision` 用 `asyncio.wait_for` 强制 `VISION_CALL_TIMEOUT`(30s) 超时，超时后记录调用并返回 None |
| ROI 排除区域不适配不同 viewport | `utils/image_diff.py` | `compute_pixel_diff_roi` 从固定 60px 改为比例制 `exclude_top_frac=0.05` / `exclude_bottom_frac=0.05` |
| 块级 diff 阈值硬编码 | `utils/image_diff.py`, `config/settings.py` | 新增 `VISION_BLOCK_DIFF_THRESHOLD=0.08` 配置项，`screenshots_differ` 从 settings 读取 |
| Shadow DOM 递归无深度限制 | `utils/perception_scripts.py` | 3 处 `querySelectorAllDeep`/`querySelectorDeep` 均加 `depth` 参数，上限 10 层 |
| 健康检查只报不治 | `agents/browser_execution.py` | 命中率=0 且无 fallback 命中时跳过 CSS 提取，直接走视觉兜底 |
| `validate_selectors` 不穿透 Shadow DOM | `utils/search_engine_profiles.py` | JS 从 `document.querySelectorAll` 改为内联 `querySelectorAllDeep` |
