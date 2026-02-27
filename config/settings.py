"""
OmniCore 全局配置模块
"""
import os
from pathlib import Path
from dotenv import load_dotenv

# 加载环境变量
load_dotenv()


class Settings:
    """全局配置类"""

    # === 项目路径 ===
    PROJECT_ROOT = Path(__file__).parent.parent
    DATA_DIR = PROJECT_ROOT / "data"

    # === 大模型配置 ===
    DEFAULT_MODEL = os.getenv("DEFAULT_MODEL", "deepseek/deepseek-chat")
    VISION_MODEL = os.getenv("VISION_MODEL", "gpt-4o")  # 多模态模型
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
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
