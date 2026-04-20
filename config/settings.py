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
    ZHIPU_API_KEY = os.getenv("ZHIPU_API_KEY", "")

    # === Embedding 语义匹配配置 ===
    ZHIPU_EMBEDDING_MODEL = os.getenv("ZHIPU_EMBEDDING_MODEL", "embedding-3")
    # 语义匹配：文本分块大小（字符数）
    RELEVANCE_CHUNK_SIZE = max(_env_int("RELEVANCE_CHUNK_SIZE", 256), 64)
    # 语义匹配：块之间的重叠字符数（必须小于 chunk_size）
    RELEVANCE_CHUNK_OVERLAP = min(
        _env_int("RELEVANCE_CHUNK_OVERLAP", 64),
        max(_env_int("RELEVANCE_CHUNK_SIZE", 256), 64) - 1,
    )
    # 语义匹配：返回的 top-k 最相关块数量
    RELEVANCE_TOP_K = _env_int("RELEVANCE_TOP_K", 8)
    # 语义匹配：文本总长度低于此值时跳过匹配直接返回全文
    RELEVANCE_MIN_TEXT_LENGTH = _env_int("RELEVANCE_MIN_TEXT_LENGTH", 1500)

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
    WEB_PERCEPTION_DEBUG = os.getenv("WEB_PERCEPTION_DEBUG", "false").lower() == "true"
    WEB_PERCEPTION_DEBUG_DIR = Path(
        os.getenv("WEB_PERCEPTION_DEBUG_DIR", DATA_DIR / "debug" / "web_perception")
    )

    # === Vision Perception (浏览器视觉感知) ===
    VISION_PERCEPTION_MODEL = os.getenv("VISION_PERCEPTION_MODEL", "gemini/gemini-2.0-flash")
    VISION_PERCEPTION_ENABLED = os.getenv("VISION_PERCEPTION_ENABLED", "true").lower() == "true"
    VISION_PERCEPTION_COMPLEXITY_THRESHOLD = float(os.getenv("VISION_PERCEPTION_COMPLEXITY_THRESHOLD", "0.4"))
    VISION_ON_NEW_PAGE = os.getenv("VISION_ON_NEW_PAGE", "true").lower() == "true"
    VISION_VERIFY_ACTION = os.getenv("VISION_VERIFY_ACTION", "true").lower() == "true"
    VISION_PIXEL_DIFF_THRESHOLD = float(os.getenv("VISION_PIXEL_DIFF_THRESHOLD", "0.05"))
    VISION_WAIT_CHANGE_DETECT = os.getenv("VISION_WAIT_CHANGE_DETECT", "true").lower() == "true"
    VISION_PROGRESS_WINDOW = int(os.getenv("VISION_PROGRESS_WINDOW", "3"))
    # Vision budget / rate-limiting
    VISION_MAX_CALLS_PER_RUN = int(os.getenv("VISION_MAX_CALLS_PER_RUN", "5"))
    VISION_COOLDOWN_SECONDS = float(os.getenv("VISION_COOLDOWN_SECONDS", "3.0"))
    VISION_MAX_TOKENS_PER_RUN = int(os.getenv("VISION_MAX_TOKENS_PER_RUN", "20000"))
    VISION_CALL_TIMEOUT = int(os.getenv("VISION_CALL_TIMEOUT", "30000"))  # ms, 短于全局超时
    VISION_BLOCK_DIFF_THRESHOLD = float(os.getenv("VISION_BLOCK_DIFF_THRESHOLD", "0.08"))

    # === 页面感知配置 ===
    # 传给 LLM 的主文本字符上限（detail/list/serp 页面）
    MAIN_TEXT_LIMIT_DETAIL = _env_int("MAIN_TEXT_LIMIT_DETAIL", 4000)
    # 传给 LLM 的主文本字符上限（其他页面）
    MAIN_TEXT_LIMIT_DEFAULT = _env_int("MAIN_TEXT_LIMIT_DEFAULT", 2400)
    # Modal 判定：主内容区低于此字符数时，才将页面类型标记为 modal
    MODAL_CONTENT_THRESHOLD = _env_int("MODAL_CONTENT_THRESHOLD", 200)

    # === LLM 上下文预算 ===
    # 动作决策总 token 预算（所有区块共享）
    ACTION_DECISION_CONTEXT_TOKENS = _env_int("ACTION_DECISION_CONTEXT_TOKENS", 6000)
    # 页面评估总 token 预算
    PAGE_ASSESSMENT_CONTEXT_TOKENS = _env_int("PAGE_ASSESSMENT_CONTEXT_TOKENS", 2400)
    # 各区块预算：(min_chars, max_chars, weight)
    DATA_BUDGET_MIN_CHARS = _env_int("DATA_BUDGET_MIN_CHARS", 800)
    DATA_BUDGET_MAX_CHARS = _env_int("DATA_BUDGET_MAX_CHARS", 4000)
    CARDS_BUDGET_MIN_CHARS = _env_int("CARDS_BUDGET_MIN_CHARS", 600)
    CARDS_BUDGET_MAX_CHARS = _env_int("CARDS_BUDGET_MAX_CHARS", 3200)
    ELEMENTS_BUDGET_MIN_CHARS = _env_int("ELEMENTS_BUDGET_MIN_CHARS", 600)
    ELEMENTS_BUDGET_MAX_CHARS = _env_int("ELEMENTS_BUDGET_MAX_CHARS", 3600)

    # === 感知展示限制（传给 LLM 的格式化截断） ===
    # 卡片提取与展示
    MAX_EXTRACT_CARDS = _env_int("MAX_EXTRACT_CARDS", 14)
    CARD_TITLE_DISPLAY_CHARS = _env_int("CARD_TITLE_DISPLAY_CHARS", 160)
    CARD_SOURCE_DISPLAY_CHARS = _env_int("CARD_SOURCE_DISPLAY_CHARS", 60)
    CARD_SNIPPET_DISPLAY_CHARS = _env_int("CARD_SNIPPET_DISPLAY_CHARS", 500)
    # 元素展示
    ELEMENT_TEXT_DISPLAY_CHARS = _env_int("ELEMENT_TEXT_DISPLAY_CHARS", 120)
    ELEMENT_ATTR_DISPLAY_CHARS = _env_int("ELEMENT_ATTR_DISPLAY_CHARS", 80)
    ELEMENT_HREF_DISPLAY_CHARS = _env_int("ELEMENT_HREF_DISPLAY_CHARS", 160)
    ELEMENT_DISPLAY_LIMIT = _env_int("ELEMENT_DISPLAY_LIMIT", 30)
    # 文本块展示
    TEXT_BLOCKS_DISPLAY_LIMIT = _env_int("TEXT_BLOCKS_DISPLAY_LIMIT", 18)
    TEXT_BLOCK_DISPLAY_CHARS = _env_int("TEXT_BLOCK_DISPLAY_CHARS", 400)

    # === 浏览器决策配置 ===
    # 传给 LLM 的最近步骤数
    BROWSER_LLM_RECENT_STEPS = _env_int("BROWSER_LLM_RECENT_STEPS", 6)
    # 反思机制开关
    BROWSER_REFLECTION_ENABLED = os.getenv("BROWSER_REFLECTION_ENABLED", "true").lower() == "true"
    # 触发反思的连续失败阈值
    BROWSER_REFLECTION_FAIL_THRESHOLD = _env_int("BROWSER_REFLECTION_FAIL_THRESHOLD", 2)

    # === 浏览器自我规划优化（P0 指纹去重 + P1 任务级 Plan + P2 Prompt 合一）===
    # 指纹窗口大小：保留最近 N 步执行指纹用于去重
    BROWSER_STEP_MEMORY_SIZE = _env_int("BROWSER_STEP_MEMORY_SIZE", 20)
    # 同指纹重复多少次后拒绝再次执行
    BROWSER_DEDUP_THRESHOLD = _env_int("BROWSER_DEDUP_THRESHOLD", 2)
    # Prompt 中注入的最近步数（替代原 BROWSER_LLM_RECENT_STEPS 的默认值）
    BROWSER_RECENT_STEPS_IN_PROMPT = _env_int("BROWSER_RECENT_STEPS_IN_PROMPT", 8)
    # P1 任务级 Plan
    BROWSER_PLAN_ENABLED = os.getenv("BROWSER_PLAN_ENABLED", "true").lower() == "true"
    BROWSER_MAX_PLAN_STEPS = _env_int("BROWSER_MAX_PLAN_STEPS", 8)
    BROWSER_MAX_REPLANS = _env_int("BROWSER_MAX_REPLANS", 2)
    BROWSER_STEP_STUCK_THRESHOLD = _env_int("BROWSER_STEP_STUCK_THRESHOLD", 4)
    # P2 单 Prompt（browser_act.txt）决策开关：默认关闭，待实地回放稳定后可切换为 true
    BROWSER_UNIFIED_ACT_ENABLED = os.getenv("BROWSER_UNIFIED_ACT_ENABLED", "false").lower() == "true"
    # P3 跨会话长期记忆（默认关闭）
    BROWSER_PLAN_MEMORY_ENABLED = os.getenv("BROWSER_PLAN_MEMORY_ENABLED", "false").lower() == "true"
    # B6 三模式解耦（默认关闭,稳定后切换）
    BROWSER_STRATEGY_REFACTOR_ENABLED = os.getenv(
        "BROWSER_STRATEGY_REFACTOR_ENABLED", "false"
    ).lower() == "true"

    # === B1 站点选择器 + 登录流持久化 ===
    BROWSER_SITE_KNOWLEDGE_DB = os.getenv(
        "BROWSER_SITE_KNOWLEDGE_DB", str(DATA_DIR / "site_knowledge.db")
    )
    BROWSER_SELECTOR_HINT_TOP_K = max(_env_int("BROWSER_SELECTOR_HINT_TOP_K", 5), 1)
    BROWSER_LOGIN_REPLAY_ENABLED = os.getenv("BROWSER_LOGIN_REPLAY_ENABLED", "true").lower() == "true"
    BROWSER_SELECTOR_MIN_SUCCESS_RATE = float(os.getenv("BROWSER_SELECTOR_MIN_SUCCESS_RATE", "0.6"))
    BROWSER_SELECTOR_DECAY_DAYS = max(_env_int("BROWSER_SELECTOR_DECAY_DAYS", 30), 1)
    BROWSER_SITE_HINTS_INJECT = os.getenv("BROWSER_SITE_HINTS_INJECT", "true").lower() == "true"
    # B1 执行层:fallback 链前置 site_hint 策略(需 BROWSER_PLAN_MEMORY_ENABLED=true)
    BROWSER_SITE_HINTS_EXEC_INJECT = os.getenv(
        "BROWSER_SITE_HINTS_EXEC_INJECT", "true"
    ).lower() == "true"

    # === B5 失败策略自适应学习 ===
    BROWSER_STRATEGY_LEARNING_ENABLED = os.getenv(
        "BROWSER_STRATEGY_LEARNING_ENABLED", "false"
    ).lower() == "true"
    BROWSER_STRATEGY_DB = os.getenv(
        "BROWSER_STRATEGY_DB", str(DATA_DIR / "browser_strategy.db")
    )
    BROWSER_STRATEGY_MIN_SAMPLES = max(_env_int("BROWSER_STRATEGY_MIN_SAMPLES", 5), 1)
    BROWSER_STRATEGY_SKIP_THRESHOLD = float(
        os.getenv("BROWSER_STRATEGY_SKIP_THRESHOLD", "0.1")
    )

    # === B2 反爬 domain 画像 + 自适应节流 ===
    ANTI_BOT_PROFILE_ENABLED = os.getenv("ANTI_BOT_PROFILE_ENABLED", "false").lower() == "true"
    ANTI_BOT_PROFILE_DB = os.getenv(
        "ANTI_BOT_PROFILE_DB", str(DATA_DIR / "anti_bot.db")
    )
    ANTI_BOT_INITIAL_DELAY_SEC = float(os.getenv("ANTI_BOT_INITIAL_DELAY_SEC", "0"))
    ANTI_BOT_MAX_DELAY_SEC = float(os.getenv("ANTI_BOT_MAX_DELAY_SEC", "5"))
    ANTI_BOT_UA_POOL_FILE = os.getenv("ANTI_BOT_UA_POOL_FILE", "config/ua_pool.yaml")
    ANTI_BOT_BLOCK_DECAY_DAYS = max(_env_int("ANTI_BOT_BLOCK_DECAY_DAYS", 14), 1)
    ANTI_BOT_SUCCESS_TO_COOLDOWN = max(_env_int("ANTI_BOT_SUCCESS_TO_COOLDOWN", 5), 1)

    # === B4 iframe / 多 tab 支持 ===
    # Enables the decision layer to see {available_frames}/{available_tabs} in prompts.
    # Execution path for SWITCH_IFRAME / SWITCH_TAB / CLOSE_TAB is always on
    # (existed before B4); these flags only control prompt injection and
    # heuristic fallbacks.
    BROWSER_IFRAME_ENABLED = os.getenv("BROWSER_IFRAME_ENABLED", "true").lower() == "true"
    BROWSER_TAB_MANAGEMENT_ENABLED = os.getenv("BROWSER_TAB_MANAGEMENT_ENABLED", "true").lower() == "true"
    # Auto-scan non-main frames when a click/input gets stuck on the main frame.
    # Default off — first rely on the LLM choosing SWITCH_IFRAME from the prompt.
    BROWSER_IFRAME_AUTO_SCAN_ON_STUCK = os.getenv(
        "BROWSER_IFRAME_AUTO_SCAN_ON_STUCK", "false"
    ).lower() == "true"
    # Per-context tab cap; oldest non-active tab is closed when exceeded.
    # Set to 0 to disable.
    BROWSER_MAX_TAB_COUNT = max(_env_int("BROWSER_MAX_TAB_COUNT", 10), 0)

    # === B3 视觉描述缓存 ===
    BROWSER_VISION_CACHE_ENABLED = os.getenv("BROWSER_VISION_CACHE_ENABLED", "false").lower() == "true"
    BROWSER_VISION_CACHE_DB = os.getenv(
        "BROWSER_VISION_CACHE_DB", str(DATA_DIR / "vision_cache.db")
    )
    BROWSER_VISION_CACHE_TTL_DAYS = max(_env_int("BROWSER_VISION_CACHE_TTL_DAYS", 7), 1)
    BROWSER_VISION_CACHE_BYPASS_KEYWORDS = os.getenv(
        "BROWSER_VISION_CACHE_BYPASS_KEYWORDS",
        "login,signin,sign-in,password,payment,checkout,verify,auth,2fa,otp",
    )

    # === P4 批量执行与按需纠偏 ===
    BROWSER_BATCH_EXECUTE_ENABLED = os.getenv("BROWSER_BATCH_EXECUTE_ENABLED", "false").lower() == "true"
    BROWSER_SEQUENCE_MODEL = os.getenv("BROWSER_SEQUENCE_MODEL", "")
    BROWSER_MAX_SEQUENCE_ACTIONS = _env_int("BROWSER_MAX_SEQUENCE_ACTIONS", 10)
    BROWSER_MAX_CORRECTIONS = _env_int("BROWSER_MAX_CORRECTIONS", 2)
    BROWSER_DOM_CHECKPOINT_ENABLED = os.getenv("BROWSER_DOM_CHECKPOINT_ENABLED", "true").lower() == "true"
    BROWSER_VISUAL_VERIFY_ENABLED = os.getenv("BROWSER_VISUAL_VERIFY_ENABLED", "true").lower() == "true"
    BROWSER_CORRECTION_ESCALATE_TO_REASONING = os.getenv("BROWSER_CORRECTION_ESCALATE_TO_REASONING", "true").lower() == "true"

    # === 搜索结果与文本相关性评分权重 ===
    # 文本相关性：token 匹配 / 字符 n-gram 重叠 / 数字匹配
    TEXT_RELEVANCE_WEIGHT_TOKEN = float(os.getenv("TEXT_RELEVANCE_WEIGHT_TOKEN", "0.55"))
    TEXT_RELEVANCE_WEIGHT_NGRAM = float(os.getenv("TEXT_RELEVANCE_WEIGHT_NGRAM", "0.30"))
    TEXT_RELEVANCE_WEIGHT_NUMBER = float(os.getenv("TEXT_RELEVANCE_WEIGHT_NUMBER", "0.15"))
    TEXT_RELEVANCE_STRONG_HIT_MULTIPLIER = float(os.getenv("TEXT_RELEVANCE_STRONG_HIT_MULTIPLIER", "1.25"))
    # 搜索结果卡片排名
    SEARCH_RANK_WEIGHT_RELEVANCE = float(os.getenv("SEARCH_RANK_WEIGHT_RELEVANCE", "0.65"))
    SEARCH_RANK_WEIGHT_AUTHORITY = float(os.getenv("SEARCH_RANK_WEIGHT_AUTHORITY", "0.20"))
    SEARCH_RANK_BONUS_BASE = float(os.getenv("SEARCH_RANK_BONUS_BASE", "0.12"))
    SEARCH_RANK_BONUS_DECAY = float(os.getenv("SEARCH_RANK_BONUS_DECAY", "0.01"))
    # 来源权威性加分
    SEARCH_AUTHORITY_BONUS_GOV_EDU_ORG = float(os.getenv("SEARCH_AUTHORITY_BONUS_GOV_EDU_ORG", "2.0"))
    SEARCH_AUTHORITY_MAX = float(os.getenv("SEARCH_AUTHORITY_MAX", "6.0"))
    # 元素优先级评分
    ELEMENT_SCORE_TASK_TOKEN_MATCH = float(os.getenv("ELEMENT_SCORE_TASK_TOKEN_MATCH", "2.0"))
    ELEMENT_SCORE_INPUT_TYPE = float(os.getenv("ELEMENT_SCORE_INPUT_TYPE", "1.0"))
    ELEMENT_SCORE_NOT_VISIBLE = float(os.getenv("ELEMENT_SCORE_NOT_VISIBLE", "-2.0"))
    ELEMENT_SCORE_NOT_CLICKABLE = float(os.getenv("ELEMENT_SCORE_NOT_CLICKABLE", "-1.5"))
    ELEMENT_SCORE_HAS_PLACEHOLDER = float(os.getenv("ELEMENT_SCORE_HAS_PLACEHOLDER", "0.8"))
    ELEMENT_SCORE_HAS_LABEL = float(os.getenv("ELEMENT_SCORE_HAS_LABEL", "0.8"))
    ELEMENT_SCORE_BUTTON_LINK = float(os.getenv("ELEMENT_SCORE_BUTTON_LINK", "0.4"))

    # === 浏览器执行配置 ===
    # 连续失败容忍次数：连续多少次操作失败后放弃任务
    BROWSER_MAX_CONSECUTIVE_FAILS = _env_int("BROWSER_MAX_CONSECUTIVE_FAILS", 4)
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

    # === 多模态输入/输出 ===
    MULTIMODAL_IMAGE_ENABLED = os.getenv("MULTIMODAL_IMAGE_ENABLED", "true").lower() == "true"
    MULTIMODAL_AUDIO_ENABLED = os.getenv("MULTIMODAL_AUDIO_ENABLED", "false").lower() == "true"
    MULTIMODAL_DOCUMENT_ENABLED = os.getenv("MULTIMODAL_DOCUMENT_ENABLED", "true").lower() == "true"
    VOICE_OUTPUT_ENABLED = os.getenv("VOICE_OUTPUT_ENABLED", "false").lower() == "true"
    VOICE_OUTPUT_MODEL = os.getenv("VOICE_OUTPUT_MODEL", "tts-1")
    VOICE_OUTPUT_VOICE = os.getenv("VOICE_OUTPUT_VOICE", "alloy")

    # === MCP (Model Context Protocol) ===
    MCP_ENABLED = os.getenv("MCP_ENABLED", "true").lower() == "true"
    MCP_TOOL_CALL_TIMEOUT = _env_int("MCP_TOOL_CALL_TIMEOUT", 30)

    # === Knowledge Base (RAG) ===
    KNOWLEDGE_BASE_ENABLED = os.getenv("KNOWLEDGE_BASE_ENABLED", "true").lower() == "true"
    KNOWLEDGE_RETRIEVAL_TOP_K = _env_int("KNOWLEDGE_RETRIEVAL_TOP_K", 5)
    KNOWLEDGE_MAX_CONTEXT_CHARS = _env_int("KNOWLEDGE_MAX_CONTEXT_CHARS", 4000)
    KNOWLEDGE_DISTANCE_THRESHOLD = float(os.getenv("KNOWLEDGE_DISTANCE_THRESHOLD", "0.5"))
    KNOWLEDGE_MIN_CONTENT_LENGTH = _env_int("KNOWLEDGE_MIN_CONTENT_LENGTH", 50)

    # === Prompt Section Registry（S1）===
    # Section 级 prompt 缓存开关
    PROMPT_SECTION_CACHE_ENABLED = os.getenv("PROMPT_SECTION_CACHE_ENABLED", "true").lower() == "true"
    # System prompt 总 token 预算（0=不限制）
    PROMPT_TOKEN_BUDGET = _env_int("PROMPT_TOKEN_BUDGET", 4000)
    # 是否输出 prompt section 详细 token 报告
    DEBUG_PROMPT = os.getenv("DEBUG_PROMPT", "false").lower() == "true"

    # === Tool Pipeline（S4）===
    # 严格模式：校验失败直接拒绝而非降级执行
    TOOL_PIPELINE_STRICT_MODE = os.getenv("TOOL_PIPELINE_STRICT_MODE", "false").lower() == "true"
    # 是否启用 Pipeline（false 时走旧路径，用于渐进迁移）
    TOOL_PIPELINE_ENABLED = os.getenv("TOOL_PIPELINE_ENABLED", "true").lower() == "true"

    # === Session Event Sourcing（S3）===
    # Event sourcing 开关（默认关闭，双写模式：event log + 原 snapshot）
    SESSION_EVENT_LOG_ENABLED = os.getenv("SESSION_EVENT_LOG_ENABLED", "false").lower() == "true"
    # Event 批量 flush 间隔（秒）
    SESSION_EVENT_FLUSH_INTERVAL = _env_int("SESSION_EVENT_FLUSH_INTERVAL", 5)

    # === 多 Agent 协作（S5）===
    # 是否启用 Coordinator 模式（复杂任务自动拆分为子 agent 并行执行）
    COORDINATOR_ENABLED = os.getenv("COORDINATOR_ENABLED", "false").lower() == "true"
    # 子 agent 最大嵌套深度（防止无限递归）
    MAX_SUBAGENT_DEPTH = max(_env_int("MAX_SUBAGENT_DEPTH", 1), 1)
    # 同时运行的子 agent 数量上限
    MAX_PARALLEL_SUBAGENTS = max(_env_int("MAX_PARALLEL_SUBAGENTS", 3), 1)
    # 单个子 agent 最大执行轮次
    SUBAGENT_MAX_TURNS = max(_env_int("SUBAGENT_MAX_TURNS", 10), 1)
    # 单个子 agent 超时时间（秒）
    SUBAGENT_TIMEOUT = max(_env_int("SUBAGENT_TIMEOUT", 300), 30)
    # 子 agent 失败策略: fail_fast（取消其余）或 best_effort（继续其余）
    SUBAGENT_FAILURE_STRATEGY = os.getenv("SUBAGENT_FAILURE_STRATEGY", "best_effort").strip().lower()

    # === Fail-Closed 安全分层（S6）===
    # MCP 工具描述最大字符数（超出自动截断）
    MCP_DESCRIPTION_MAX_LENGTH = _env_int("MCP_DESCRIPTION_MAX_LENGTH", 2048)
    # MCP 工具默认信任等级（builtin / local / mcp_local / mcp_remote）
    MCP_TRUST_LEVEL = os.getenv("MCP_TRUST_LEVEL", "mcp_local").strip().lower()
    # 网络请求域名白名单（逗号分隔，空=不限制）
    ALLOWED_DOMAINS = _env_csv("ALLOWED_DOMAINS")
    # 是否启用工具执行审计日志（data/audit/{date}.jsonl）
    AUDIT_LOG_ENABLED = os.getenv("AUDIT_LOG_ENABLED", "true").lower() == "true"
    # MCP Server 认证失败缓存时间（秒，防认证雪崩）
    MCP_AUTH_FAILURE_CACHE_SECONDS = _env_int("MCP_AUTH_FAILURE_CACHE_SECONDS", 900)
    # MCP Server 单个连接超时（秒）
    MCP_CONNECT_TIMEOUT = _env_int("MCP_CONNECT_TIMEOUT", 30)
    # MCP Server startup 总超时（秒）
    MCP_STARTUP_TIMEOUT = _env_int("MCP_STARTUP_TIMEOUT", 60)

    # === E1 Prompt Injection 防护 ===
    # 启发式检测主开关:关闭后 wrap_untrusted 仍会包裹但跳过检测/事件
    PROMPT_INJECTION_DETECT_ENABLED = os.getenv("PROMPT_INJECTION_DETECT_ENABLED", "true").lower() == "true"
    # 高风险命中是否抛 PromptInjectionBlocked(默认仅标记不阻断)
    PROMPT_INJECTION_BLOCK_ON_HIGH = os.getenv("PROMPT_INJECTION_BLOCK_ON_HIGH", "false").lower() == "true"
    # 可选 LLM 判定(预留接口,实际未接入)
    PROMPT_INJECTION_LLM_JUDGE = os.getenv("PROMPT_INJECTION_LLM_JUDGE", "false").lower() == "true"
    PROMPT_INJECTION_SAMPLE_RATE = float(os.getenv("PROMPT_INJECTION_SAMPLE_RATE", "0.1"))
    # security event 落盘路径(相对路径基于 PROJECT_ROOT)
    PROMPT_INJECTION_EVENT_LOG = os.getenv("PROMPT_INJECTION_EVENT_LOG", "data/security_events.jsonl")

    # === 上下文预算制 + 压缩重注入（S2）===
    # 为 auto-compact 预留的 token 数
    CONTEXT_RESERVE_TOKENS = _env_int("CONTEXT_RESERVE_TOKENS", 20000)
    # 触发 compact 的上下文使用率阈值
    CONTEXT_COMPACT_THRESHOLD = float(os.getenv("CONTEXT_COMPACT_THRESHOLD", "0.85"))
    # Compact 连续失败熔断次数
    COMPACT_MAX_CONSECUTIVE_FAILURES = _env_int("COMPACT_MAX_CONSECUTIVE_FAILURES", 3)

    # === 上下文成本控制（R1）===
    # 工具返回结果最大字符数（超出则截断，保留头 60% + 尾 30%）
    TOOL_RESULT_MAX_CHARS = max(_env_int("TOOL_RESULT_MAX_CHARS", 8000), 500)
    # 历史消息最大条数（超出则触发 snip）
    HISTORY_MAX_MESSAGES = max(_env_int("HISTORY_MAX_MESSAGES", 20), 5)
    # 保留最近完整消息数（其余消息内容截断到 200 字符）
    HISTORY_KEEP_RECENT = max(_env_int("HISTORY_KEEP_RECENT", 10), 1)

    # === Plan Mode 持久化与 Reminder（R5）===
    # 是否将规划结果持久化为 Markdown 文件（data/plans/{job_id}.md）
    PLAN_PERSISTENCE_ENABLED = os.getenv("PLAN_PERSISTENCE_ENABLED", "true").lower() == "true"
    # 连续多少轮未有任务状态变化时注入计划提醒
    PLAN_REMINDER_INTERVAL = max(_env_int("PLAN_REMINDER_INTERVAL", 5), 1)

    # === Session Memory 后台提炼（R7）===
    # 是否启用 session memory 定期提炼（默认关闭，需手动开启）
    SESSION_MEMORY_ENABLED = os.getenv("SESSION_MEMORY_ENABLED", "false").lower() == "true"
    # 每隔多少轮触发一次 session memory 提炼
    SESSION_MEMORY_INTERVAL = max(_env_int("SESSION_MEMORY_INTERVAL", 8), 2)

    # === MessageBus 配置（R2）===
    # 消息 TTL 秒数（0=不过期，默认 30 分钟）
    MESSAGE_BUS_TTL = _env_int("MESSAGE_BUS_TTL", 1800)
    # 消息最大容量（超出时删除最旧消息）
    MESSAGE_BUS_MAX_CAPACITY = max(_env_int("MESSAGE_BUS_MAX_CAPACITY", 500), 50)

    # === 成本感知智能路由 ===
    COST_TRACKING_ENABLED = os.getenv("COST_TRACKING_ENABLED", "true").lower() == "true"
    MONTHLY_BUDGET_USD = float(os.getenv("MONTHLY_BUDGET_USD", "0"))  # 0 表示不限制
    COMPLEXITY_AWARE_ROUTING = os.getenv("COMPLEXITY_AWARE_ROUTING", "true").lower() == "true"

    # === 事件驱动信息流 ===
    EVENT_DRIVEN_ENABLED = os.getenv("EVENT_DRIVEN_ENABLED", "true").lower() == "true"
    WEB_WATCH_MIN_INTERVAL = _env_int("WEB_WATCH_MIN_INTERVAL", 300)  # 最小检查间隔（秒）
    WEB_WATCH_DEFAULT_INTERVAL = _env_int("WEB_WATCH_DEFAULT_INTERVAL", 3600)  # 默认检查间隔
    WEB_WATCH_DEFAULT_THRESHOLD = float(os.getenv("WEB_WATCH_DEFAULT_THRESHOLD", "0.1"))
    WEBHOOK_ENABLED = os.getenv("WEBHOOK_ENABLED", "false").lower() == "true"
    WEBHOOK_PORT = _env_int("WEBHOOK_PORT", 9988)

    # === Skill Library ===
    SKILL_LIBRARY_ENABLED = os.getenv("SKILL_LIBRARY_ENABLED", "true").lower() == "true"
    SKILL_MATCH_THRESHOLD = float(os.getenv("SKILL_MATCH_THRESHOLD", "0.3"))
    SKILL_MIN_STEPS_TO_EXTRACT = _env_int("SKILL_MIN_STEPS_TO_EXTRACT", 2)
    SKILL_AUTO_DEPRECATE_THRESHOLD = float(os.getenv("SKILL_AUTO_DEPRECATE_THRESHOLD", "0.3"))
    SKILL_AUTO_DEPRECATE_MIN_USES = _env_int("SKILL_AUTO_DEPRECATE_MIN_USES", 3)
    # A3: inject top-k skills as hints into router even when no exact match
    SKILL_HINT_ENABLED = os.getenv("SKILL_HINT_ENABLED", "true").lower() == "true"
    SKILL_HINT_TOP_K = _env_int("SKILL_HINT_TOP_K", 3)
    SKILL_HINT_MIN_SCORE = float(os.getenv("SKILL_HINT_MIN_SCORE", "0.4"))

    # === 记忆衰减与归档（A1）===
    MEMORY_DECAY_ENABLED = os.getenv("MEMORY_DECAY_ENABLED", "true").lower() == "true"
    MEMORY_HALF_LIFE_DAYS = float(os.getenv("MEMORY_HALF_LIFE_DAYS", "30"))
    MEMORY_RERANK_POOL_MULTIPLIER = max(_env_int("MEMORY_RERANK_POOL_MULTIPLIER", 3), 1)
    MEMORY_TTL_DAYS = _env_int("MEMORY_TTL_DAYS", 90)
    MEMORY_CONSOLIDATION_ENABLED = os.getenv("MEMORY_CONSOLIDATION_ENABLED", "false").lower() == "true"
    MEMORY_CONSOLIDATION_MODEL = os.getenv("MEMORY_CONSOLIDATION_MODEL", "")
    MEMORY_CONSOLIDATION_BATCH_SIZE = max(_env_int("MEMORY_CONSOLIDATION_BATCH_SIZE", 10), 2)
    MEMORY_CONSOLIDATION_MIN_HITS = max(_env_int("MEMORY_CONSOLIDATION_MIN_HITS", 1), 0)

    # === 记忆实体倒排索引（A2）===
    MEMORY_ENTITY_INDEX_ENABLED = os.getenv("MEMORY_ENTITY_INDEX_ENABLED", "true").lower() == "true"
    MEMORY_ENTITY_INDEX_COLLECTION = os.getenv("MEMORY_ENTITY_INDEX_COLLECTION", "omnicore_entities")
    MEMORY_INJECT_TOP_ENTITIES = os.getenv("MEMORY_INJECT_TOP_ENTITIES", "false").lower() == "true"
    MEMORY_ENTITY_TOP_K = max(_env_int("MEMORY_ENTITY_TOP_K", 5), 1)

    # === 记忆分层（A4）===
    # 默认开启:TieredMemoryStore 在命中为空时回退读 legacy,保证切换无数据断层
    MEMORY_TIERED_ENABLED = os.getenv("MEMORY_TIERED_ENABLED", "true").lower() == "true"
    MEMORY_TIER_LEGACY_FALLBACK = os.getenv("MEMORY_TIER_LEGACY_FALLBACK", "true").lower() == "true"
    MEMORY_TIER_LEGACY_COLLECTION = os.getenv("MEMORY_TIER_LEGACY_COLLECTION", "omnicore_memory")
    MEMORY_TIER_WORKING_COLLECTION = os.getenv("MEMORY_TIER_WORKING_COLLECTION", "omnicore_working")
    MEMORY_TIER_EPISODIC_COLLECTION = os.getenv("MEMORY_TIER_EPISODIC_COLLECTION", "omnicore_episodic")
    MEMORY_TIER_SEMANTIC_COLLECTION = os.getenv("MEMORY_TIER_SEMANTIC_COLLECTION", "omnicore_semantic")
    MEMORY_TIER_WEIGHT_WORKING = float(os.getenv("MEMORY_TIER_WEIGHT_WORKING", "1.0"))
    MEMORY_TIER_WEIGHT_EPISODIC = float(os.getenv("MEMORY_TIER_WEIGHT_EPISODIC", "1.0"))
    MEMORY_TIER_WEIGHT_SEMANTIC = float(os.getenv("MEMORY_TIER_WEIGHT_SEMANTIC", "1.2"))

    # === Episodic Replay 跨会话轨迹注入（C1）===
    # 默认 off：避免冷启动期空表/噪声轨迹影响 router；积累一段时间后再开
    EPISODE_REPLAY_ENABLED = os.getenv("EPISODE_REPLAY_ENABLED", "false").lower() == "true"
    EPISODE_REPLAY_DB = os.getenv("EPISODE_REPLAY_DB", str(DATA_DIR / "episodes.db"))
    EPISODE_REPLAY_TOP_K = max(_env_int("EPISODE_REPLAY_TOP_K", 2), 1)
    EPISODE_REPLAY_MAX_AGE_DAYS = max(_env_int("EPISODE_REPLAY_MAX_AGE_DAYS", 60), 1)
    EPISODE_REPLAY_MIN_SIMILARITY = float(os.getenv("EPISODE_REPLAY_MIN_SIMILARITY", "0.45"))
    EPISODE_REPLAY_MAX_DAG_STEPS = max(_env_int("EPISODE_REPLAY_MAX_DAG_STEPS", 8), 2)

    # === Tool Failure Auto-Tune (C2) ===
    # 通用 per-tool 失败画像：tool_pipeline 末端打点，router/planner 注入近期健康度
    # 默认 off：积累一段时间样本后再开，避免冷启动期空表噪声
    TOOL_FAILURE_PROFILE_ENABLED = os.getenv("TOOL_FAILURE_PROFILE_ENABLED", "false").lower() == "true"
    TOOL_FAILURE_PROFILE_DB = os.getenv(
        "TOOL_FAILURE_PROFILE_DB", str(DATA_DIR / "tool_failure.db")
    )
    # 滑动窗口：每个 tool 只保留最近 N 次执行结果用于成功率/超时率计算
    TOOL_FAILURE_WINDOW = max(_env_int("TOOL_FAILURE_WINDOW", 20), 3)
    # router 注入门槛：至少有 N 次样本才输出健康提示
    TOOL_FAILURE_MIN_SAMPLES = max(_env_int("TOOL_FAILURE_MIN_SAMPLES", 5), 1)
    # router 注入门槛：timeout_rate 超此值 planner 应绕开/调高超时
    TOOL_FAILURE_SKIP_THRESHOLD = float(os.getenv("TOOL_FAILURE_SKIP_THRESHOLD", "0.7"))
    # router 注入门槛：fail_rate 超此值算"近期不可靠"
    TOOL_FAILURE_WARN_THRESHOLD = float(os.getenv("TOOL_FAILURE_WARN_THRESHOLD", "0.5"))
    # router 注入：最多注入 N 条工具健康提示
    TOOL_FAILURE_HINT_TOP_K = max(_env_int("TOOL_FAILURE_HINT_TOP_K", 5), 1)

    # === 用户偏好自动学习（A5）===
    # 默认开启:通过 persist_job_outcome 末尾的 gated 触发器周期性推断,成本极低
    PREFERENCE_LEARNING_ENABLED = os.getenv("PREFERENCE_LEARNING_ENABLED", "true").lower() == "true"
    # 自动触发节流:距上次运行 < N 小时不重复执行
    PREFERENCE_LEARNING_MIN_INTERVAL_HOURS = max(_env_int("PREFERENCE_LEARNING_MIN_INTERVAL_HOURS", 24), 1)
    PREFERENCE_LEARNING_WINDOW_DAYS = max(_env_int("PREFERENCE_LEARNING_WINDOW_DAYS", 7), 1)
    PREFERENCE_LEARNING_MIN_CONFIDENCE = float(os.getenv("PREFERENCE_LEARNING_MIN_CONFIDENCE", "0.6"))
    PREFERENCE_LEARNING_MODEL = os.getenv("PREFERENCE_LEARNING_MODEL", "")
    PREFERENCE_LEARNING_MIN_SAMPLES = max(_env_int("PREFERENCE_LEARNING_MIN_SAMPLES", 5), 2)
    PREFERENCE_INJECT_TO_ROUTER = os.getenv("PREFERENCE_INJECT_TO_ROUTER", "false").lower() == "true"

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

    # === 终端执行配置 ===
    # 是否启用终端 Worker（Claude Code 级别的 shell 执行能力）
    TERMINAL_ENABLED = os.getenv("TERMINAL_ENABLED", "true").lower() == "true"
    # 默认执行超时（秒），比 system_worker 的 30s 更宽松
    TERMINAL_DEFAULT_TIMEOUT = _env_int("TERMINAL_DEFAULT_TIMEOUT", 120)
    # 最大超时上限（秒）
    TERMINAL_MAX_TIMEOUT = _env_int("TERMINAL_MAX_TIMEOUT", 600)
    # 是否启用沙箱目录限制（限制写操作只能在沙箱根目录内）
    TERMINAL_SANDBOX_ENABLED = os.getenv("TERMINAL_SANDBOX_ENABLED", "false").lower() == "true"
    # 沙箱根目录（写操作只能在此目录下进行）
    TERMINAL_SANDBOX_ROOT = os.getenv("TERMINAL_SANDBOX_ROOT", str(Path.cwd()))
    # 是否实时流式输出命令执行结果
    TERMINAL_STREAM_OUTPUT = os.getenv("TERMINAL_STREAM_OUTPUT", "true").lower() == "true"
    # 使用哪个 shell 执行命令
    TERMINAL_SHELL = os.getenv("TERMINAL_SHELL", os.environ.get("SHELL", "/bin/zsh"))
    # 权限模式：strict（同 system_worker）/ balanced（三级权限）/ permissive（只确认危险操作）
    TERMINAL_PERMISSION_MODE = os.getenv("TERMINAL_PERMISSION_MODE", "balanced")
    # 会话内记住已审批的命令类别，同类操作不重复确认
    TERMINAL_SESSION_APPROVALS = os.getenv("TERMINAL_SESSION_APPROVALS", "true").lower() == "true"
    # 用户自定义自动放行的命令前缀（逗号分隔）
    TERMINAL_AUTO_ALLOW_PATTERNS = _env_csv("TERMINAL_AUTO_ALLOW_PATTERNS")
    # 用户自定义强制确认的命令前缀（逗号分隔）
    TERMINAL_ALWAYS_CONFIRM_PATTERNS = _env_csv("TERMINAL_ALWAYS_CONFIRM_PATTERNS")

    # === Memory 查询去重缓存 (F4) ===
    MEMORY_QUERY_CACHE_ENABLED = os.getenv("MEMORY_QUERY_CACHE_ENABLED", "true").lower() == "true"
    MEMORY_QUERY_CACHE_TTL_SEC = _env_int("MEMORY_QUERY_CACHE_TTL_SEC", 60)

    # === Browser 直答透传 (F3) ===
    # 是否从 page_assessment reason 提取 answer_text 并透传到 Finalizer
    BROWSER_ANSWER_TEXT_ENABLED = os.getenv("BROWSER_ANSWER_TEXT_ENABLED", "true").lower() == "true"
    # Finalizer 是否把 answer_text 前置到数据表之前
    FINALIZER_ANSWER_FIRST = os.getenv("FINALIZER_ANSWER_FIRST", "true").lower() == "true"
    # answer_citations 最多返回条数
    FINALIZER_MAX_CITATIONS = _env_int("FINALIZER_MAX_CITATIONS", 3)

    # === Browser 数据污染治理 (F2) ===
    # page_main_text fallback 截断阈值（字符数）
    BROWSER_FALLBACK_TEXT_MAX_LEN = _env_int("BROWSER_FALLBACK_TEXT_MAX_LEN", 800)
    # 低于此长度的 main_text 不作为 fallback 写入
    BROWSER_FALLBACK_TEXT_MIN_LEN = _env_int("BROWSER_FALLBACK_TEXT_MIN_LEN", 50)

    # === FileWorker 配置 ===
    # CSV 流式写入触发阈值（行数超过此值自动切换分批写入，避免 OOM）
    FILE_STREAM_THRESHOLD = _env_int("FILE_STREAM_THRESHOLD", 50_000)
    # CSV 流式写入每批行数
    FILE_STREAM_CHUNK_SIZE = _env_int("FILE_STREAM_CHUNK_SIZE", 10_000)
    # LLM 文档生成最大 tokens（generate 模式）
    FILE_GENERATE_MAX_TOKENS = _env_int("FILE_GENERATE_MAX_TOKENS", 4096)


settings = Settings()
