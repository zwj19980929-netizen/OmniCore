
# 🚀 Bootstrap Directive: Project OmniCore MVP

你现在是我的首席 AI 架构师。我将交给你一份完整的系统需求文档（PRD & Architecture Spec）。
在开始写代码之前，请严格遵守以下开发纪律：

1. 绝对服从分步执行： 请严格按照文档中的 Execution_Phases_for_AI 阶段执行。现在，你只需要执行 Step 1（输出目录结构和 requirements.txt）。千万不要提前输出 Step 2 及以后的代码。
2. 拒绝占位符： 所有的代码必须是可以实际运行的，特别是在处理 First_Test_Case 时，必须写出真实的 DOM 定位和真实的文件写入逻辑。
3. 核心状态锁定： 为了确保 LangGraph 的多智能体状态不丢失，请在后续编写核心逻辑时，强制使用以下 TypedDict 作为图的底层 State：

--- Python 代码开始 ---
from typing import TypedDict, Annotated, List, Dict, Any
from langgraph.graph.message import add_messages

class OmniCoreState(TypedDict):
messages: Annotated[list, add_messages] # 记录所有 Agent 之间的对话和系统提示
current_intent: str                     # 路由器的当前意图解析
task_queue: List[str]                   # 子任务队列 (DAG 的执行顺序)
shared_memory: Dict[str, Any]           # 各个 Worker 抓取或处理后的中间数据暂存区
critic_feedback: str                    # 独立审查官 (Critic) 的反馈
human_approved: bool                    # 强制安全锁：执行高危操作前必须为 True
error_trace: str                        # 错误日志追踪
--- Python 代码结束 ---

如果你理解了以上纪律，请仔细阅读下方的需求文档，并立即为我输出 Step 1 的结果。

=========================================
[附：系统需求文档]

# 🤖 System Prompt & Architecture Spec: Project "OmniCore MVP"

## Context

你现在是一位顶级的全栈 AI 架构师和资深 Python/Rust 工程师。你的任务是根据本需求文档，为我从零开发一个基于“智能体蜂群（Agent Swarm）”和“本地多模态执行”的桌面级 AI 助手 MVP。
请不要给我泛泛的建议，而是直接提供可执行的代码结构、依赖配置和模块化代码。

## Core_Philosophy

这个助手不仅是聊天工具，而是“Actionable Agent（可执行智能体）”。它必须具备以下特性：

1. 多 Agent 协同： 使用路由机制分配任务，而不是单模型跑到底。
2. 免 API 执行： 能够在本地通过操控浏览器或系统 UI 节点来执行任务，降低对第三方 API 的依赖。
3. 本地记忆： 具备本地向量数据库，能记录用户习惯。

## Tech_Stack_Requirements

请严格基于以下技术栈进行架构设计和代码生成：

* 核心逻辑编排： LangGraph 或 AutoGen（用于构建 Agent 路由和有向无环图 DAG）。
* 大模型接口： LiteLLM（支持无缝切换 OpenAI, Anthropic, Gemini 等模型）。
* 免接口自动化执行：
* Web 端：Playwright (Python)
* 桌面端：PyAutoGUI 结合 Windows UIAutomation / macOS Accessibility API


* 本地记忆中心： ChromaDB 或 SQLite + 简单 Embedding。
* 前端交互 (可选)： Streamlit 或 PyQt6（保持极简，主打后台静默运行和指令输入）。

## Architecture_Modules

请按照以下四大核心模块为我搭建系统：

### Module 1: 主脑路由器 (The Router Agent)

* 功能： 接收用户的自然语言指令，识别意图，并将其拆解为一个或多个子任务。
* 行为： 决定是调用“网页浏览 Agent”、“本地文件分析 Agent”还是“系统控制 Agent”。

### Module 2: 执行蜂群 (Worker Agents)

* Web Worker: 接收 URL 和目标，使用 Playwright 隐式打开浏览器，抓取数据或模拟点击提交。
* File Worker: 接收本地路径，读取 PDF/Excel/CSV，进行数据提取和清洗。
* System Worker: 将复杂操作转化为 Python 脚本并沙盒执行，或通过 PyAutoGUI 模拟键鼠。

### Module 3: 独立审查官 (Critic Agent)

* 功能： 在最终结果返回给用户或执行高危操作（如发送邮件、删除文件）前，进行逻辑校验。如果发现错误，打回给 Worker 重新执行。

### Module 4: 记忆挂载 (Context Memory)

* 功能： 每次对话和执行结果，提取关键 Entity（实体）存入 ChromaDB。Router Agent 在拆解任务前，必须先查询历史记忆。

## First_Test_Case

为了确保系统闭环且 MVP 能够实际运行，请严格基于以下具体场景来编写核心业务代码，而不是给我写占位符（Mock）：

* 用户输入的测试指令： "去 Hacker News (news.ycombinator.com) 抓取排名前 5 的新闻标题和链接，然后把结果保存到我桌面的 news_summary.txt 文件里。"
* 期望的路由与执行流：
1. Router: 分析指令，将其拆解为两个动作（网页抓取 + 本地文件写入）。
2. Web Worker: 使用 Playwright 打开网页，定位并提取前 5 条数据。
3. File Worker: 接收数据，使用 Python 本地操作创建并写入 txt 文件。
4. Critic: 检查 txt 文件是否成功生成且内容不为空。



## Execution_Phases_for_AI

请按照以下步骤与我交互并输出代码：

* Step 1: 首先，输出项目的整体目录结构树，以及 requirements.txt。等待我确认。
* Step 2: 在我确认后，先编写 Router Agent 和 LiteLLM 的连接核心代码。
* Step 3: 编写 Web Worker 的 Playwright 自动化测试脚本样例。
* Step 4: 将所有模块通过 LangGraph 串联起来，形成闭环。

## Security_Constraint

* 必须在执行任何系统级文件修改或网络发送操作前，加入 human_in_the_loop (人类确认) 中断机制。


