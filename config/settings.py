"""
OmniCore 全局配置模块
"""
import os
from pathlib import Path
from dotenv import load_dotenv

# 加载环境变量
load_dotenv()


def _set_env_pair(name: str, value: str) -> None:
    if value:
        os.environ[name] = value
        os.environ[name.lower()] = value
    else:
        os.environ.pop(name, None)
        os.environ.pop(name.lower(), None)


def _apply_managed_proxy_env(
    *,
    allow_system_proxy: bool,
    http_proxy: str = "",
    https_proxy: str = "",
    all_proxy: str = "",
    no_proxy: str = "",
) -> None:
    if not allow_system_proxy:
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"):
            _set_env_pair(key, "")

    if http_proxy:
        _set_env_pair("HTTP_PROXY", http_proxy)
    if https_proxy:
        _set_env_pair("HTTPS_PROXY", https_proxy)
    if all_proxy:
        _set_env_pair("ALL_PROXY", all_proxy)
    if no_proxy:
        _set_env_pair("NO_PROXY", no_proxy)


_apply_managed_proxy_env(
    allow_system_proxy=os.getenv("ALLOW_SYSTEM_PROXY", "false").lower() == "true",
    http_proxy=os.getenv("OMNICORE_HTTP_PROXY", "").strip(),
    https_proxy=os.getenv("OMNICORE_HTTPS_PROXY", "").strip(),
    all_proxy=os.getenv("OMNICORE_ALL_PROXY", "").strip(),
    no_proxy=os.getenv("OMNICORE_NO_PROXY", "").strip(),
)

PROJECT_ROOT = Path(__file__).parent.parent
DATA_DIR = PROJECT_ROOT / "data"
RUNTIME_METRICS_OVERRIDE_PATH = Path(
    os.getenv("RUNTIME_METRICS_OVERRIDE_PATH", DATA_DIR / "runtime_metrics_overrides.env")
)
RUNTIME_METRICS_TUNING_KEYS = {
    "BROWSER_POOL_ACQUIRE_TIMEOUT_SECONDS",
    "BROWSER_POOL_IDLE_TTL_SECONDS",
    "BROWSER_POOL_MAX_BROWSERS_PER_KEY",
    "BROWSER_POOL_MAX_CONTEXTS_PER_BROWSER",
    "LLM_CACHE_INFLIGHT_WAIT_SECONDS",
    "LLM_CACHE_PAGE_ANALYSIS_MAX_ENTRIES",
    "LLM_CACHE_URL_ANALYSIS_MAX_ENTRIES",
    "PAGE_ANALYSIS_CACHE_TTL_SECONDS",
    "URL_ANALYSIS_CACHE_TTL_SECONDS",
}


def _load_runtime_metrics_overrides() -> None:
    if not RUNTIME_METRICS_OVERRIDE_PATH.exists():
        return
    try:
        with RUNTIME_METRICS_OVERRIDE_PATH.open("r", encoding="utf-8") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                if key in RUNTIME_METRICS_TUNING_KEYS:
                    os.environ[key] = value.strip()
    except OSError:
        return


_load_runtime_metrics_overrides()


def _env_int(name: str, default: int) -> int:
    """读取整型环境变量，异常时回退默认值。"""
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_csv(name: str) -> tuple[str, ...]:
    """Parse a delimited environment variable into a normalized tuple."""
    raw = os.getenv(name, "")
    if not raw:
        return ()

    values = []
    for part in raw.replace(";", ",").split(","):
        item = part.strip()
        if item:
            values.append(item)
    return tuple(values)


class Settings:
    """全局配置类"""

    # === 项目路径 ===
    PROJECT_ROOT = PROJECT_ROOT
    DATA_DIR = DATA_DIR
    RUNTIME_METRICS_OVERRIDE_PATH = RUNTIME_METRICS_OVERRIDE_PATH

    # === 大模型配置 ===
    DEFAULT_MODEL = os.getenv("DEFAULT_MODEL", "deepseek/deepseek-chat")
    VISION_MODEL = os.getenv("VISION_MODEL", "gpt-4o")  # 多模态模型
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
    OPENAI_API_BASE = os.getenv("OPENAI_API_BASE", "")  # OpenAI 代理地址
    ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
    GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
    DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
    KIMI_API_KEY = os.getenv("KIMI_API_KEY", "")
    MINIMAX_API_KEY = os.getenv("MINIMAX_API_KEY", "")
    ALLOW_SYSTEM_PROXY = os.getenv("ALLOW_SYSTEM_PROXY", "false").lower() == "true"
    OMNICORE_HTTP_PROXY = os.getenv("OMNICORE_HTTP_PROXY", "").strip()
    OMNICORE_HTTPS_PROXY = os.getenv("OMNICORE_HTTPS_PROXY", "").strip()
    OMNICORE_ALL_PROXY = os.getenv("OMNICORE_ALL_PROXY", "").strip()
    OMNICORE_NO_PROXY = os.getenv("OMNICORE_NO_PROXY", "").strip()

    # === 模型智能路由 ===
    PREFERRED_PROVIDER = os.getenv("PREFERRED_PROVIDER", "")  # gemini/kimi/openai/deepseek/minimax
    COST_PREFERENCE = os.getenv("COST_PREFERENCE", "low")  # low/medium/high
    MODELS_CONFIG_PATH = os.getenv("MODELS_CONFIG_PATH", "config/models.yaml")

    # === LLM 调用配置 ===
    # 默认 max_tokens，可通过环境变量 LLM_MAX_TOKENS 配置
    # 建议值：65535（最大），32768（平衡），16000（默认）
    LLM_MAX_TOKENS = _env_int("LLM_MAX_TOKENS", 65535)
    # Router 专用 max_tokens（路由分析通常需要更多 tokens）
    LLM_ROUTER_MAX_TOKENS = _env_int("LLM_ROUTER_MAX_TOKENS", 65535)
    # 普通对话 max_tokens
    LLM_CHAT_MAX_TOKENS = _env_int("LLM_CHAT_MAX_TOKENS", 32768)

    # === 本地路径 ===
    USER_DESKTOP_PATH = Path(
        os.getenv("USER_DESKTOP_PATH", Path.home() / "Desktop")
    )
    CHROMA_PERSIST_DIR = Path(
        os.getenv("CHROMA_PERSIST_DIR", DATA_DIR / "chroma")
    )

    # === 安全配置 ===
    REQUIRE_HUMAN_CONFIRM = os.getenv("REQUIRE_HUMAN_CONFIRM", "true").lower() == "true"

    # === 调试配置 ===
    DEBUG_MODE = os.getenv("DEBUG_MODE", "false").lower() == "true"
    LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

    # === 浏览器执行配置 ===
    # 快速模式：减少随机延迟与不必要等待，优先吞吐
    BROWSER_FAST_MODE = os.getenv("BROWSER_FAST_MODE", "true").lower() == "true"
    # 阻断重资源：图片/字体/媒体，减少页面负载
    BLOCK_HEAVY_RESOURCES = os.getenv("BLOCK_HEAVY_RESOURCES", "true").lower() == "true"
    # 静态抓取优先：纯读取页面时先尝试 requests，不启动浏览器
    STATIC_FETCH_ENABLED = os.getenv("STATIC_FETCH_ENABLED", "true").lower() == "true"
    BROWSER_POOL_ENABLED = os.getenv("BROWSER_POOL_ENABLED", "true").lower() == "true"
    BROWSER_POOL_IDLE_TTL_SECONDS = max(_env_int("BROWSER_POOL_IDLE_TTL_SECONDS", 120), 1)
    BROWSER_POOL_MAX_BROWSERS_PER_KEY = max(
        _env_int("BROWSER_POOL_MAX_BROWSERS_PER_KEY", 1), 1
    )
    BROWSER_POOL_MAX_ACTIVE_LEASES_PER_KEY = max(
        _env_int("BROWSER_POOL_MAX_ACTIVE_LEASES_PER_KEY", 4), 1
    )
    BROWSER_POOL_MAX_CONTEXTS_PER_BROWSER = max(
        _env_int(
            "BROWSER_POOL_MAX_CONTEXTS_PER_BROWSER",
            _env_int("BROWSER_POOL_MAX_ACTIVE_LEASES_PER_KEY", 4),
        ),
        1,
    )
    BROWSER_POOL_ACQUIRE_TIMEOUT_SECONDS = max(
        _env_int("BROWSER_POOL_ACQUIRE_TIMEOUT_SECONDS", 10), 1
    )
    BROWSER_POOL_CIRCUIT_BREAK_THRESHOLD = max(
        _env_int("BROWSER_POOL_CIRCUIT_BREAK_THRESHOLD", 3), 1
    )
    BROWSER_POOL_CIRCUIT_BREAK_SECONDS = max(
        _env_int("BROWSER_POOL_CIRCUIT_BREAK_SECONDS", 30), 1
    )
    # DAG 调度是否允许同批次并行执行互不依赖的任务
    ENABLE_PARALLEL_EXECUTION = os.getenv("ENABLE_PARALLEL_EXECUTION", "true").lower() == "true"
    # 单批次最大并行任务数
    MAX_PARALLEL_TASKS = max(_env_int("MAX_PARALLEL_TASKS", 4), 1)
    # 浏览器任务单批次并行上限，避免 Playwright 资源争抢过重
    MAX_PARALLEL_BROWSER_TASKS = max(_env_int("MAX_PARALLEL_BROWSER_TASKS", 2), 1)
    # 系统任务默认串行，避免命令执行/桌面控制互相干扰
    MAX_PARALLEL_SYSTEM_TASKS = max(_env_int("MAX_PARALLEL_SYSTEM_TASKS", 1), 1)

    # === 超时配置（统一管理，单位：毫秒）===
    # 浏览器操作超时
    BROWSER_NAVIGATION_TIMEOUT = _env_int("BROWSER_NAVIGATION_TIMEOUT", 30000)  # 页面导航
    BROWSER_LOAD_TIMEOUT = _env_int("BROWSER_LOAD_TIMEOUT", 10000)  # 页面加载
    BROWSER_SELECTOR_TIMEOUT = _env_int("BROWSER_SELECTOR_TIMEOUT", 8000)  # 元素查找
    BROWSER_ACTION_TIMEOUT = _env_int("BROWSER_ACTION_TIMEOUT", 5000)  # 点击/输入等操作
    BROWSER_DOWNLOAD_TIMEOUT = _env_int("BROWSER_DOWNLOAD_TIMEOUT", 10000)  # 下载等待

    # 网络请求超时
    HTTP_REQUEST_TIMEOUT = _env_int("HTTP_REQUEST_TIMEOUT", 15000)  # HTTP 请求（毫秒）
    LLM_REQUEST_TIMEOUT = _env_int("LLM_REQUEST_TIMEOUT", 60000)  # LLM API 调用（毫秒）

    # 系统命令超时（单位：秒）
    SYSTEM_COMMAND_TIMEOUT = _env_int("SYSTEM_COMMAND_TIMEOUT", 30)  # 系统命令执行

    # LLM analysis cache
    LLM_CACHE_ENABLED = os.getenv("LLM_CACHE_ENABLED", "true").lower() == "true"
    LLM_CACHE_MAX_ENTRIES = max(_env_int("LLM_CACHE_MAX_ENTRIES", 512), 1)
    LLM_CACHE_URL_ANALYSIS_MAX_ENTRIES = max(
        _env_int("LLM_CACHE_URL_ANALYSIS_MAX_ENTRIES", 128), 1
    )
    LLM_CACHE_PAGE_ANALYSIS_MAX_ENTRIES = max(
        _env_int("LLM_CACHE_PAGE_ANALYSIS_MAX_ENTRIES", 256), 1
    )
    LLM_CACHE_INFLIGHT_WAIT_SECONDS = max(
        _env_int("LLM_CACHE_INFLIGHT_WAIT_SECONDS", 15), 1
    )
    URL_ANALYSIS_CACHE_TTL_SECONDS = max(_env_int("URL_ANALYSIS_CACHE_TTL_SECONDS", 1800), 1)
    PAGE_ANALYSIS_CACHE_TTL_SECONDS = max(_env_int("PAGE_ANALYSIS_CACHE_TTL_SECONDS", 1800), 1)
    TOOL_ADAPTER_PLUGIN_MODULES = _env_csv("TOOL_ADAPTER_PLUGIN_MODULES")
    TOOL_ADAPTER_PLUGIN_DIRS = _env_csv("TOOL_ADAPTER_PLUGIN_DIRS")
    ENABLED_TOOL_PLUGIN_IDS = _env_csv("ENABLED_TOOL_PLUGIN_IDS")
    DISABLED_TOOL_PLUGIN_IDS = _env_csv("DISABLED_TOOL_PLUGIN_IDS")
    RUNTIME_METRICS_HISTORY_LIMIT = max(_env_int("RUNTIME_METRICS_HISTORY_LIMIT", 200), 1)
    QUEUE_WORKER_MODE = os.getenv("QUEUE_WORKER_MODE", "process").strip().lower() or "process"
    QUEUE_WORKER_POLL_INTERVAL_SECONDS = max(_env_int("QUEUE_WORKER_POLL_INTERVAL_SECONDS", 1), 1)
    QUEUE_STALE_AFTER_SECONDS = max(_env_int("QUEUE_STALE_AFTER_SECONDS", 120), 5)
    SCHEDULER_RELEASE_LIMIT = max(_env_int("SCHEDULER_RELEASE_LIMIT", 5), 1)
    SCHEDULE_DEFAULT_LOOKAHEAD_SECONDS = max(_env_int("SCHEDULE_DEFAULT_LOOKAHEAD_SECONDS", 60), 1)
    NOTIFICATION_HISTORY_LIMIT = max(_env_int("NOTIFICATION_HISTORY_LIMIT", 300), 20)
    DEFAULT_OUTPUT_DIRECTORY = os.getenv("DEFAULT_OUTPUT_DIRECTORY", "")
    DEFAULT_USER_LOCATION = os.getenv("DEFAULT_USER_LOCATION", "").strip()
    DEFAULT_PREFERRED_TOOLS = _env_csv("DEFAULT_PREFERRED_TOOLS")
    DEFAULT_PREFERRED_SITES = _env_csv("DEFAULT_PREFERRED_SITES")
    DEFAULT_AUTO_QUEUE_CONFIRMATIONS = os.getenv("DEFAULT_AUTO_QUEUE_CONFIRMATIONS", "false").lower() == "true"

    # === 意图分类 ===
    INTENT_TYPES = [
        "web_scraping",      # 网页抓取
        "file_operation",    # 文件操作
        "system_control",    # 系统控制
        "data_analysis",     # 数据分析
        "information_query", # 信息查询
        "multi_step_task",   # 多步骤复合任务
    ]

    # === 高危操作列表 ===
    HIGH_RISK_OPERATIONS = [
        "delete_file",
        "send_email",
        "execute_script",
        "modify_system",
        "transfer_money",
        "post_to_social",
    ]


settings = Settings()
