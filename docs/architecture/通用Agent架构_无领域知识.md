# 🧠 通用Web Agent架构 - 无需领域知识

## 问题分析

你指出的核心问题：
- **领域知识方法** = 为每个领域手动写知识 → 不可扩展
- **Skill方法** = 为每个任务写skill → 不可扩展

两者都是**硬编码的变种**，违反了系统的通用性原则。

## 🎯 真正通用的解决方案

### 核心思想：让Agent自己推理，而不是喂给它答案

```
❌ 错误方式：告诉Agent "CNNVD首页没有列表，点击'漏洞库'进入"
✅ 正确方式：让Agent观察页面，自己推理出"这是首页，需要导航到列表页"
```

## 🏗️ 三层智能架构

### Layer 1: 结构化感知（Structured Perception）

**目标**：提供清晰、紧凑的页面表示

```python
# utils/accessibility_tree_extractor.py (已实现)
elements = await extractor.extract_tree(page)
context = extractor.to_llm_context(elements)

# 输出示例：
"""
# 页面可交互元素

## NAV区域
[lin-1] link: 首页
[lin-2] link: 漏洞库
[lin-3] link: 安全公告

## MAIN区域
[btn-1] button: 搜索
[inp-1] textbox: 请输入关键词
"""
```

**优势**：
- ✅ 紧凑（2-5KB vs 50KB HTML）
- ✅ 语义化（role + name）
- ✅ 可操作（ref引用）

### Layer 2: 自适应推理（Adaptive Reasoning）

**目标**：让Agent自己分析页面类型和导航策略

```python
# agents/reasoning_browser_agent.py

class ReasoningBrowserAgent:
    """基于推理的浏览器Agent - 不需要领域知识"""

    async def _analyze_current_situation(self, task: str,
                                        elements_context: str,
                                        current_url: str) -> Dict:
        """
        让LLM分析当前情况并制定策略

        这是关键：不告诉它答案，而是让它自己观察和推理
        """

        prompt = f"""你是一个网页导航专家。请分析当前情况。

**任务目标**: {task}

**当前URL**: {current_url}

**页面上的可交互元素**:
{elements_context}

请按以下步骤分析（Chain-of-Thought推理）：

### Step 1: 页面类型识别
观察页面元素，判断这是什么类型的页面？
- 首页/门户页（有导航菜单，但没有数据列表）
- 列表页（有多条数据项）
- 详情页（单个项目的详细信息）
- 搜索结果页
- 表单页

### Step 2: 任务需求分析
要完成任务"{task}"，我需要什么类型的页面？
- 如果任务是"获取列表"，我需要列表页
- 如果任务是"查询信息"，我可能需要搜索页或详情页

### Step 3: 差距分析
当前页面类型 vs 需要的页面类型：
- 如果匹配：可以开始提取数据
- 如果不匹配：需要导航

### Step 4: 导航策略
如果需要导航，观察可用元素，哪个最可能通向目标？
- 查找包含关键词的链接（如"列表"、"查询"、"搜索"）
- 考虑元素的位置（导航栏的链接通常是主要入口）
- 推理元素的语义（"漏洞库"可能包含漏洞列表）

### Step 5: 决策
返回JSON格式的决策：
{{
    "page_type": "homepage",  // 当前页面类型
    "target_page_type": "list_page",  // 需要的页面类型
    "needs_navigation": true,  // 是否需要导航
    "reasoning": "当前是首页，有导航菜单但没有数据列表。任务需要获取漏洞列表，所以需要点击'漏洞库'链接进入列表页。",
    "action": {{
        "type": "click",
        "target_ref": "lin-2",
        "reason": "'漏洞库'链接最可能通向漏洞列表页"
    }}
}}

重要原则：
1. 基于观察，不要猜测
2. 如果不确定，选择最可能的选项
3. 解释你的推理过程
"""

        response = await self.llm.chat(
            prompt,
            temperature=0.3,
            json_mode=True
        )

        return json.loads(response)

    async def run(self, task: str, start_url: str, max_steps: int = 10):
        """执行任务（基于推理，无需领域知识）"""

        await self.toolkit.navigate(start_url)

        for step in range(max_steps):
            # 1. 提取页面结构
            elements = await self.a11y_extractor.extract_tree(self.toolkit.page)
            context = self.a11y_extractor.to_llm_context(elements)

            current_url = await self.toolkit.get_current_url()

            # 2. 让Agent分析和推理
            analysis = await self._analyze_current_situation(
                task, context, current_url
            )

            log_agent_action(f"推理结果: {analysis['reasoning']}")

            # 3. 执行决策
            action = analysis['action']

            if action['type'] == 'done':
                # 提取数据
                return await self._extract_data(task, elements)

            elif action['type'] == 'click':
                ref = action['target_ref']
                element = self.a11y_extractor.get_element_by_ref(ref)
                if element:
                    await self.toolkit.click(element.selector)
                    await asyncio.sleep(2)  # 等待页面加载

            # ... 其他操作

        return {"success": False, "error": "达到最大步数"}
```

### Layer 3: 经验记忆（Experience Memory）

**目标**：从成功/失败中学习，避免重复错误

```python
# utils/experience_memory.py

class ExperienceMemory:
    """
    经验记忆系统

    记录Agent的成功和失败经验，用于未来任务
    不是硬编码的领域知识，而是从实际执行中学习
    """

    def __init__(self, storage_path: str):
        self.storage_path = storage_path
        self.experiences = self._load_experiences()

    def save_experience(self, task: str, url: str,
                       actions: List[Dict], result: Dict):
        """
        保存一次任务执行的经验

        Args:
            task: 任务描述
            url: 起始URL
            actions: 执行的动作序列
            result: 最终结果（成功/失败）
        """
        experience = {
            "task": task,
            "url": url,
            "domain": urlparse(url).netloc,
            "actions": actions,
            "success": result.get("success", False),
            "timestamp": datetime.now().isoformat(),
            "error": result.get("error", "")
        }

        # 提取模式
        pattern = self._extract_pattern(task, url, actions)
        experience["pattern"] = pattern

        self.experiences.append(experience)
        self._save_to_disk()

    def find_similar_experience(self, task: str, url: str) -> Optional[str]:
        """
        查找类似任务的成功经验

        Returns:
            经验提示（如果有）
        """
        domain = urlparse(url).netloc

        # 查找同域名的成功经验
        similar = [
            exp for exp in self.experiences
            if exp["domain"] == domain and exp["success"]
        ]

        if not similar:
            return None

        # 找最相似的
        best_match = max(similar,
                        key=lambda x: self._similarity(task, x["task"]))

        # 生成提示
        hint = f"""
基于之前的成功经验：
- 任务: {best_match['task']}
- 成功的导航模式: {best_match['pattern']}
- 关键步骤: {self._summarize_actions(best_match['actions'])}

你可以参考这个模式，但要根据当前页面的实际情况调整。
"""
        return hint

    def _extract_pattern(self, task: str, url: str,
                        actions: List[Dict]) -> str:
        """
        从动作序列中提取模式

        例如：
        - "首页 → 点击导航链接 → 列表页 → 提取数据"
        - "搜索页 → 输入关键词 → 点击搜索 → 结果页"
        """
        pattern_steps = []
        for action in actions:
            if action['type'] == 'click':
                pattern_steps.append(f"点击{action.get('element_role', '元素')}")
            elif action['type'] == 'input':
                pattern_steps.append("输入文本")
            elif action['type'] == 'extract':
                pattern_steps.append("提取数据")

        return " → ".join(pattern_steps)
```

## 🎯 完整工作流程

```python
# 整合所有层次

class UniversalBrowserAgent:
    """通用浏览器Agent - 无需领域知识"""

    def __init__(self, llm_client: LLMClient):
        self.llm = llm_client
        self.a11y_extractor = AccessibilityTreeExtractor()
        self.experience_memory = ExperienceMemory("data/experiences.json")
        self.toolkit = None

    async def run(self, task: str, start_url: str, max_steps: int = 10):
        """执行任务"""

        # 1. 检查是否有类似经验
        experience_hint = self.experience_memory.find_similar_experience(
            task, start_url
        )

        # 2. 导航到起始页
        await self.toolkit.navigate(start_url)

        actions_taken = []

        for step in range(max_steps):
            # 3. 提取页面结构（Layer 1）
            elements = await self.a11y_extractor.extract_tree(
                self.toolkit.page
            )
            context = self.a11y_extractor.to_llm_context(elements)

            current_url = await self.toolkit.get_current_url()

            # 4. 推理和决策（Layer 2）
            analysis = await self._analyze_and_decide(
                task=task,
                context=context,
                current_url=current_url,
                experience_hint=experience_hint,  # 可选的经验提示
                step=step
            )

            log_agent_action(f"Step {step}: {analysis['reasoning']}")

            # 5. 执行动作
            action = analysis['action']
            actions_taken.append(action)

            if action['type'] == 'done':
                result = await self._extract_data(task, elements)

                # 6. 保存经验（Layer 3）
                self.experience_memory.save_experience(
                    task, start_url, actions_taken, result
                )

                return result

            # 执行其他动作...
            await self._execute_action(action, elements)

        # 失败也要记录
        result = {"success": False, "error": "max steps reached"}
        self.experience_memory.save_experience(
            task, start_url, actions_taken, result
        )

        return result
```

## 📊 对比：领域知识 vs 自适应推理

| 维度 | 领域知识方法 | 自适应推理方法 |
|------|-------------|---------------|
| **可扩展性** | ❌ 每个新领域需要手动添加 | ✅ 自动适应新网站 |
| **维护成本** | ❌ 网站改版需要更新知识 | ✅ 自动适应变化 |
| **通用性** | ❌ 只适用于预定义领域 | ✅ 适用于任何网站 |
| **学习能力** | ❌ 不会从失败中学习 | ✅ 积累经验 |
| **代码复杂度** | 中等 | 中等 |
| **LLM要求** | 较低 | 较高（需要推理能力） |

## 🚀 实施优先级

### Phase 1: 基础推理（立即实施）
1. ✅ Accessibility Tree提取（已完成）
2. ⏳ 实现推理式决策（`_analyze_current_situation`）
3. ⏳ 测试基本任务（CNNVD、天气）

### Phase 2: 经验记忆（可选）
1. ⏳ 实现经验存储
2. ⏳ 实现经验检索
3. ⏳ 测试学习效果

### Phase 3: 视觉增强（高级）
1. ⏳ 视觉grounding（复杂场景）
2. ⏳ 多模态推理

## 💡 关键洞察

**不要试图教会Agent所有领域知识，而是教会它如何思考和学习。**

这就像：
- ❌ 给学生一本答案书（领域知识）
- ✅ 教学生解题方法（推理能力）

Agent应该像人一样：
1. 观察页面（accessibility tree）
2. 分析情况（这是什么页面？我需要什么？）
3. 制定策略（如何到达目标？）
4. 执行并学习（记住成功的模式）

## 🎯 预期效果

使用推理方法后：
- CNNVD任务：Agent会自己发现"首页没有列表，需要点击导航"
- 天气任务：Agent会自己尝试不同策略（搜索、直接访问等）
- 新网站：无需添加任何配置，Agent自己探索

**真正的通用性！**
