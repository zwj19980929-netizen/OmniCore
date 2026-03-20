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

## Testing Guidelines
Pytest is configured in `pytest.ini` with `tests/` as the main test root and temp output under `data/test-runtime/`. Add unit tests as `tests/test_<feature>_unit.py` when changing isolated behavior, and extend existing integration-style tests when touching browser/runtime flows. Cover both success paths and guardrail or failure paths.

## Commit & Pull Request Guidelines
Recent history uses short imperative subjects, often with `feat:` or `fix:` prefixes, plus plain summaries like `Improve web agent search and login perception`. Match that style and keep the subject focused on one behavior change. Pull requests should explain the user-visible impact, list the verification commands you ran, note any `.env`, Playwright, or model-provider requirements, and include screenshots for `ui/` changes.

## Security & Configuration Tips
Start from `.env.example`; never commit `.env`, API keys, or generated `data/` contents. If a change alters model routing, browser automation, or approval behavior, document the new environment variables or operational risks in `README.md` or `docs/`.

## Active Architecture Upgrade (2026-03-19)
A 7-direction architecture upgrade is in progress. Infrastructure for all 7 directions has been built; **runtime integration is pending for 5 of them**. Before starting any new work, read `docs/2026-03-19-架构升级集成计划.md` for the full plan, file index, and execution order. Key new modules: `core/stage_registry.py`, `core/agent_registry.py`, `core/message_bus.py`, `core/persistence_coordinator.py`, `utils/structured_logger.py`, `agents/browser_perception.py`, `agents/browser_decision.py`, `agents/browser_execution.py`, `config/agents.yaml`.
