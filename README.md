# OmniCore - 全栈智能体操作系统核心

> 比 Claude Cowork 更底层、更自主的 AI 智能体系统

## 🚀 快速开始

### 1. 安装依赖

```bash
# 创建虚拟环境
python -m venv venv
source venv/bin/activate  # Linux/Mac
# 或 Windows:
# venv\Scripts\activate

# 安装依赖
pip install -r requirements.txt

# 安装 Playwright 浏览器
playwright install chromium
```

### 2. 配置环境变量

```bash
# 复制环境变量模板
cp .env.example .env

# 编辑 .env 文件，填入你的 API Key
```

### 3. 运行

```bash
# 交互式命令行模式
python main.py

# 或直接执行单条任务
python main.py "去 Hacker News 抓取前5条新闻保存到桌面"

# 或启动 Web UI
streamlit run ui/app.py
```

## 💬 交互式命令

进入交互模式后（`python main.py`），可以使用以下命令：

| 命令 | 说明 |
|------|------|
| `quit` / `exit` / `q` | 退出程序 |
| `memory stats` | 查看记忆系统统计信息 |
| `clear memory` | 清空所有历史记忆 |
| `Ctrl+C` | 取消当前正在执行的任务 |

除以上命令外，直接输入自然语言指令即可，例如：
- `去 CNNVD 抓取今天的漏洞数据，保存为 Excel 到桌面`
- `帮我抓取 Hacker News 前 10 条新闻`
- `对比京东和淘宝上 iPhone 16 的价格，生成报告`

## 📁 项目结构

```
OmniCore/
├── config/          # 配置模块
├── core/            # 核心逻辑（状态、路由、LLM、图编排）
├── agents/          # 智能体（Web/File/System Worker, Critic, BrowserAgent）
├── memory/          # 记忆系统（ChromaDB 向量存储）
├── utils/           # 工具（日志、人类确认、验证码处理）
├── ui/              # Streamlit 前端
├── tests/           # 测试用例
└── main.py          # 程序入口
```

## ⚙️ 环境变量配置（.env）

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `DEFAULT_MODEL` | 默认 LLM 模型 | `deepseek/deepseek-chat` |
| `VISION_MODEL` | 多模态模型（验证码识别等） | `gpt-4o` |
| `OPENAI_API_KEY` | OpenAI API Key | - |
| `DEEPSEEK_API_KEY` | DeepSeek API Key | - |
| `ANTHROPIC_API_KEY` | Anthropic API Key | - |
| `GEMINI_API_KEY` | Gemini API Key | - |
| `USER_DESKTOP_PATH` | 用户桌面路径 | `~/Desktop` |
| `REQUIRE_HUMAN_CONFIRM` | 高危操作是否需要确认 | `true` |
| `DEBUG_MODE` | 调试模式 | `false` |
| `LOG_LEVEL` | 日志级别 | `INFO` |

## 🧪 测试

```bash
# 运行 Hacker News 抓取测试
python tests/test_hackernews.py

# 独立 Playwright 测试
python tests/test_playwright_standalone.py
```

## 🔑 核心特性

- **智能体蜂群**: Router + Workers + Critic 多 Agent 协作
- **免 API 执行**: Playwright 网页自动化 + PyAutoGUI 桌面控制
- **本地记忆**: ChromaDB 向量数据库存储历史上下文
- **安全防护**: 高危操作人类确认机制
- **多格式输出**: 支持 txt / xlsx / csv / markdown / html
- **对话上下文**: 保留最近 5 轮对话，支持追问和上下文引用
- **任务可取消**: Ctrl+C 随时中断正在执行的任务
