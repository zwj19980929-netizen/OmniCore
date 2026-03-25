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
