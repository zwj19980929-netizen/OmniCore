# 🚀 推理式Browser Agent - 实施指南

## 📦 已完成的组件

### 1. 核心组件
- ✅ `utils/accessibility_tree_extractor.py` - Accessibility Tree提取器
- ✅ `agents/reasoning_browser_agent.py` - 基础推理Agent
- ✅ `utils/experience_memory.py` - 经验记忆系统
- ✅ `agents/enhanced_reasoning_browser_agent.py` - 增强版Agent（带经验学习）
- ✅ `tests/test_reasoning_agent.py` - 测试脚本

### 2. 文档
- ✅ `docs/2026现代化Web_Agent架构.md` - 架构方案
- ✅ `docs/通用Agent架构_无领域知识.md` - 设计理念

## 🎯 核心创新点

### 1. Accessibility Tree方法（借鉴OpenClaw）
```python
# 不是让LLM处理冗长的HTML，而是提供紧凑的语义化元素列表
elements = await extractor.extract_tree(page)
context = extractor.to_llm_context(elements)

# 输出示例（2KB vs 50KB HTML）：
"""
[btn-1] button: 搜索
[lin-2] link: 漏洞库
[inp-3] textbox: 请输入关键词
"""
```

### 2. Chain-of-Thought推理
```python
# 不是告诉Agent答案，而是让它自己推理
prompt = """
Step 1: 这是什么类型的页面？（观察元素）
Step 2: 任务需要什么类型的页面？
Step 3: 当前页面 vs 目标页面？
Step 4: 如何导航到目标？
Step 5: 是否陷入循环？
Step 6: 决策
"""
```

### 3. 经验学习
```python
# 从成功/失败中学习，而不是硬编码知识
experience_memory.save_experience(task, url, actions, result)
hint = experience_memory.find_similar_experience(task, url)
```

## 🚀 快速开始

### 方式1: 直接运行测试
```bash
# 测试所有任务
python tests/test_reasoning_agent.py

# 测试单个任务
python tests/test_reasoning_agent.py cnnvd
python tests/test_reasoning_agent.py weather
python tests/test_reasoning_agent.py hackernews
```

### 方式2: 在代码中使用
```python
from playwright.async_api import async_playwright
from core.llm import LLMClient
from utils.browser_toolkit import BrowserToolkit
from agents.enhanced_reasoning_browser_agent import EnhancedReasoningBrowserAgent

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        page = await browser.new_page()

        # 创建Agent
        toolkit = BrowserToolkit(page)
        llm_client = LLMClient()
        agent = EnhancedReasoningBrowserAgent(llm_client, toolkit)

        # 执行任务
        result = await agent.run(
            task="获取CNNVD最新的5个漏洞信息",
            start_url="https://www.cnnvd.org.cn/",
            max_steps=8
        )

        print(f"成功: {result['success']}")
        print(f"数据: {result.get('data')}")

        await browser.close()

asyncio.run(main())
```

### 方式3: 集成到现有系统
```python
# 在 core/graph.py 或 agents/browser_agent.py 中
from agents.enhanced_reasoning_browser_agent import EnhancedReasoningBrowserAgent

# 替换旧的browser_agent
# old_agent = BrowserAgent(...)
new_agent = EnhancedReasoningBrowserAgent(llm_client, toolkit)

result = await new_agent.run(task, url)
```

## 📊 预期效果对比

| 任务 | 旧方法（DOM文本） | 新方法（推理式） | 改进 |
|------|------------------|-----------------|------|
| CNNVD漏洞查询 | ❌ 循环失败 | ✅ 自己推理导航 | 从0% → 70%+ |
| 天气查询 | ❌ 403放弃 | ✅ 自适应策略 | 从0% → 60%+ |
| Hacker News | ✅ 50%成功 | ✅ 80%成功 | +30% |
| 上下文大小 | 10-50KB | 2-5KB | -80% |
| LLM成本 | 高 | 低 | -60% |

## 🔧 配置说明

### 1. LLM要求
- **推荐**: Claude Opus 4.6, GPT-4, Gemini 1.5 Pro
- **最低**: 支持JSON mode和长上下文的模型
- **温度**: 0.3（推理需要稳定性）

### 2. 经验存储
```python
# 默认存储路径
experience_storage_path = "data/agent_experiences.json"

# 自定义路径
agent = EnhancedReasoningBrowserAgent(
    llm_client,
    toolkit,
    experience_storage_path="custom/path/experiences.json"
)
```

### 3. 调试模式
```python
# 在 utils/logger.py 中设置日志级别
import logging
logging.basicConfig(level=logging.DEBUG)

# 查看详细的推理过程
```

## 🎓 工作原理

### 执行流程
```
1. 导航到起始URL
   ↓
2. 提取Accessibility Tree（语义化元素列表）
   ↓
3. Chain-of-Thought推理
   - 识别页面类型
   - 分析任务需求
   - 制定导航策略
   - 检测循环
   ↓
4. 执行动作（click/input/scroll）
   ↓
5. 重复2-4，直到完成或失败
   ↓
6. 保存经验（成功或失败）
```

### 推理示例
```
任务: 获取CNNVD最新漏洞

Step 1推理: 当前是首页，有导航菜单但没有数据列表
Step 2推理: 任务需要列表页
Step 3推理: 不匹配，需要导航
Step 4推理: "漏洞库"链接最可能通向列表页
Step 5推理: 没有循环
Step 6决策: 点击"漏洞库"链接

→ 执行点击 → 到达列表页 → 提取数据 → 完成
```

## 🐛 故障排查

### 问题1: LLM返回格式错误
```python
# 检查LLM是否支持JSON mode
llm_client = LLMClient()
response = await llm_client.chat(prompt, json_mode=True)
```

### 问题2: 元素定位失败
```python
# 检查accessibility tree提取
elements = await extractor.extract_tree(page)
print(f"提取到 {len(elements)} 个元素")
print(extractor.to_llm_context(elements))
```

### 问题3: 推理陷入循环
```python
# 检查action_history
for action in agent.action_history:
    print(f"Step {action['step']}: {action['action']} - {action['reasoning']}")
```

### 问题4: 经验未保存
```python
# 检查存储路径
import os
print(os.path.exists("data/agent_experiences.json"))

# 手动保存
agent.experience_memory._save_to_disk()
```

## 📈 性能优化

### 1. 减少步数
```python
# 提供更明确的任务描述
task = "访问CNNVD网站的漏洞库页面，提取最新5个漏洞"  # 更明确
# vs
task = "获取漏洞信息"  # 太模糊
```

### 2. 利用经验
```python
# 第一次执行会慢（探索）
# 第二次执行会快（利用经验）
result1 = await agent.run(task, url)  # 8步
result2 = await agent.run(task, url)  # 3步（有经验）
```

### 3. 调整max_steps
```python
# 简单任务
result = await agent.run(task, url, max_steps=5)

# 复杂任务
result = await agent.run(task, url, max_steps=15)
```

## 🔄 与现有系统集成

### 集成到Router
```python
# core/router.py
from agents.enhanced_reasoning_browser_agent import EnhancedReasoningBrowserAgent

class Router:
    async def route_browser_task(self, task, url):
        # 使用新的推理式Agent
        agent = EnhancedReasoningBrowserAgent(self.llm, self.toolkit)
        result = await agent.run(task, url)
        return result
```

### 集成到Graph
```python
# core/graph.py
async def browser_agent_node(state):
    agent = EnhancedReasoningBrowserAgent(llm_client, toolkit)
    result = await agent.run(
        task=state["current_task"],
        start_url=state["target_url"]
    )
    state["browser_result"] = result
    return state
```

## 🎯 下一步优化方向

### Phase 1: 视觉增强（可选）
- 实现视觉grounding标记
- 在复杂场景使用截图+标记
- 多模态推理

### Phase 2: 更智能的经验系统
- 经验聚类（发现通用模式）
- 跨域名迁移学习
- 自动清理无效经验

### Phase 3: 自我改进
- 从失败中学习改进策略
- A/B测试不同推理路径
- 自动调整推理prompt

## 💡 关键洞察

**不要试图教会Agent所有知识，而是教会它如何思考。**

这就像：
- ❌ 给学生答案书（领域知识）
- ✅ 教学生解题方法（推理能力）

Agent应该：
1. 观察（accessibility tree）
2. 推理（Chain-of-Thought）
3. 执行（动作）
4. 学习（经验记忆）

## 📚 参考资料

- [SeeClick: GUI Grounding](https://arxiv.org/html/2401.10935v1)
- [SeeAct: Multimodal Web Agent](https://osu-nlp-group.github.io/SeeAct/)
- [OpenClaw Browser Automation](https://lobehub.com/skills/openclaw-skills-browserautomation-skill)
- [How AI Agents See Your Website](https://www.nohackspod.com/blog/how-ai-agents-see-your-website)

## 🎉 总结

你现在有了一个**真正通用**的Web Agent：
- ✅ 无需为每个网站写代码
- ✅ 无需硬编码领域知识
- ✅ 自己推理导航策略
- ✅ 从经验中学习
- ✅ 适应网站变化

这才是2026年Web Agent的正确方向！
