"""
OmniCore 全局配置模块
"""
import os
from pathlib import Path
from dotenv import load_dotenv

# 加载环境变量
load_dotenv()


def _env_int(name: str, default: int) -> int:
    """读取整型环境变量，异常时回退默认值。"""
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


class Settings:
    """全局配置类"""

    # === 项目路径 ===
    PROJECT_ROOT = Path(__file__).parent.parent
    DATA_DIR = PROJECT_ROOT / "data"

    # === 大模型配置 ===
    DEFAULT_MODEL = os.getenv("DEFAULT_MODEL", "deepseek/deepseek-chat")
    VISION_MODEL = os.getenv("VISION_MODEL", "gpt-4o")  # 多模态模型
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
    OPENAI_API_BASE = os.getenv("OPENAI_API_BASE", "")  # OpenAI 代理地址
    ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
    GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
    DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
    KIMI_API_KEY = os.getenv("KIMI_API_KEY", "")

    # === 模型智能路由 ===
    PREFERRED_PROVIDER = os.getenv("PREFERRED_PROVIDER", "")  # gemini/kimi/openai/deepseek
    COST_PREFERENCE = os.getenv("COST_PREFERENCE", "low")  # low/medium/high
    MODELS_CONFIG_PATH = os.getenv("MODELS_CONFIG_PATH", "config/models.yaml")

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
