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
