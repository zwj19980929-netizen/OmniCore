# 🔧 修复总结 - 搜索词污染 & PUA 教练 Prompt

## 📋 问题描述

### 问题 1：搜索词被 task_id 污染
**现象：**
- web_worker 在搜索时使用了 `"task_1"` 字面量而不是实际的任务描述
- 日志显示：`[Agent: WebWorker] 搜索候选网站 -> "task_1" official vulnerability advisory`
- 用户要求搜索 CNNVD 漏洞，但搜索词变成了 `"task_1"`

**根本原因：**
- Router 生成的某些任务中，`description` 字段被错误地设置为 `task_id`（如 `"task_1"`）
- web_worker 的 `execute_async` 方法直接使用 `task["description"]` 作为 `task_description`
- 导致搜索时使用了错误的关键词

### 问题 2：PUA 教练 Prompt 不可见
**现象：**
- 用户要求看到 PUA 教练的 prompt 词
- 但 prompt 是硬编码在 `ai_pua_coach.py` 中的
- 用户无法直接查看或修改 prompt

---

## ✅ 解决方案

### 修复 1：搜索词污染问题

**文件：** `agents/web_worker.py`

**修改位置：** `execute_async` 方法（第 1802-1828 行）

**修复逻辑：**
```python
# 🔥 修复：如果 description 是 task_id 或太短，尝试从 params 中获取实际的 task 描述
if task_description and (task_description.startswith("task_") or len(task_description) < 10):
    # description 可能被错误地设置为 task_id，尝试从 params["task"] 获取
    actual_task = params.get("task", "")
    if actual_task and len(actual_task) > len(task_description):
        log_warning(f"检测到 description 可能是 task_id ('{task_description}')，使用 params['task'] 代替: {actual_task[:80]}")
        task_description = actual_task
    else:
        # 如果 params["task"] 也没有，记录警告但继续使用原 description
        log_warning(f"检测到可疑的 task description: '{task_description}'，但 params 中没有更好的替代")
```

**工作原理：**
1. 检测 `task["description"]` 是否以 `"task_"` 开头或长度小于 10
2. 如果是，尝试从 `params["task"]` 获取实际的任务描述
3. 如果 `params["task"]` 存在且更长，使用它替代
4. 记录警告日志，方便调试

**效果：**
- ✅ 即使 router 生成了错误的 description，web_worker 也能自动修正
- ✅ 搜索词将使用实际的任务描述，而不是 `"task_1"`
- ✅ 添加了调试日志，方便追踪问题

---

### 修复 2：PUA 教练 Prompt 可见化

**新增文件：** `prompts/ai_pua_coach.txt`

**内容：**
```
你是一个毒舌但有效的 AI 教练。你的任务是批评一个失败的 AI Agent，但你的批评必须：

1. **变着花样骂** - 不要用固定的话术，根据具体情况创造性地批评
2. **PUA 但有建设性** - 表面毒舌，但要指出问题并给出方向
3. **反思失败原因** - 深入分析为什么失败
4. **给出具体建议** - 告诉它下一步该怎么做

## 任务上下文
用户任务：{context}

## 当前失败的步骤
- 步骤编号：第 {step_no} 步
- 执行的动作：{action}
- 期望结果：{expected}
- 实际结果：{actual}
- 错误信息：{error}

## 历史表现
- 总步骤数：{total_steps}
- 成功次数：{success_count}
- 失败次数：{failure_count}
- 成功率：{success_rate}%

## 是否重复失败
{repeated_failure_note}

## 最近的失败记录
{recent_failures}

---

请生成一段批评，包含以下部分：

1. **开场白**（1-2 句话，要毒舌但有创意，不要用固定套路）
2. **失败原因分析**（深入分析为什么失败，要具体）
3. **反思**（让它反思自己的问题，PUA 式的质问）
4. **具体建议**（3-5 条可执行的建议，要具体到操作层面）
5. **下一步行动**（明确告诉它下一步该做什么）

要求：
- 语气要毒舌但不要重复，每次都要有新意
- 如果是重复失败，要特别严厉
- 批评要有针对性，不要泛泛而谈
- 建议要具体，不要说"改进方法"这种废话
- 用 emoji 增强表现力
- 用 markdown 格式化输出

直接输出批评内容，不要有任何前缀或解释。
```

**修改文件：** `utils/ai_pua_coach.py`

**修改内容：**
1. 添加 `_load_prompt_template()` 方法，从文件加载 prompt
2. 在 `__init__` 中加载 prompt 模板
3. 修改 `_generate_ai_pua()` 方法，使用模板而不是硬编码

**代码变更：**
```python
def __init__(self):
    self.llm = LLMClient()
    self.step_history: List[TaskStep] = []
    self.failure_count = 0
    self.success_count = 0
    self.prompt_template = self._load_prompt_template()  # 新增

def _load_prompt_template(self) -> str:
    """加载 PUA 教练的 prompt 模板"""
    prompt_path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)),
        "prompts",
        "ai_pua_coach.txt"
    )
    try:
        with open(prompt_path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        console.print(f"[yellow]警告：无法加载 PUA 教练 prompt 文件: {e}[/yellow]")
        # 返回一个简化的默认 prompt
        return """..."""  # 简化的备用 prompt

def _generate_ai_pua(self, step: TaskStep, context: str) -> str:
    # ... 前面的代码 ...

    # 使用模板构建 prompt
    prompt = self.prompt_template.format(
        context=context,
        step_no=step.step_no,
        action=step.action,
        expected=step.expected,
        actual=step.actual,
        error=step.error,
        total_steps=len(self.step_history),
        success_count=self.success_count,
        failure_count=self.failure_count,
        success_rate=f"{success_rate:.1f}",
        repeated_failure_note="是的！这个操作之前就失败过，它还在重复同样的错误！" if is_repeated else "不是重复失败",
        recent_failures=self._format_recent_failures(recent_failures)
    )
    # ... 后续代码 ...
```

**效果：**
- ✅ Prompt 现在存储在独立的文件中，用户可以直接查看和修改
- ✅ 如果文件加载失败，有备用的简化 prompt
- ✅ 保持了原有的功能，只是改变了 prompt 的存储方式

---

## 📁 修改的文件

### 修改文件
1. **`agents/web_worker.py`**
   - 修改 `execute_async` 方法
   - 添加 task_description 自动修正逻辑
   - 添加调试日志

2. **`utils/ai_pua_coach.py`**
   - 添加 `_load_prompt_template()` 方法
   - 修改 `__init__` 方法
   - 修改 `_generate_ai_pua()` 方法
   - 添加 `import os`

### 新增文件
3. **`prompts/ai_pua_coach.txt`**
   - PUA 教练的 prompt 模板
   - 包含所有占位符和格式化指令

---

## 🚀 测试方法

### 测试搜索词修复

```bash
cd /Users/zhangwenjun/zwj_project/OmniCore
source .venv/bin/activate
python main.py
```

输入测试任务：
```
到cnnvd找到当天最新的漏洞，到官网网页上去抓，我想先看你操作网页的能力，不要用bing，用谷歌去搜
```

**预期结果：**
- ✅ 搜索词应该是 "CNNVD 最新漏洞" 或类似的实际任务描述
- ✅ 不应该出现 "task_1" 或 "task_2" 这样的字面量
- ✅ 如果检测到问题，会显示警告日志

### 测试 PUA 教练 Prompt

1. **查看 Prompt：**
```bash
cat /Users/zhangwenjun/zwj_project/OmniCore/prompts/ai_pua_coach.txt
```

2. **修改 Prompt：**
- 直接编辑 `prompts/ai_pua_coach.txt` 文件
- 修改批评的语气、风格、要求等
- 重启 OmniCore，新的 prompt 会自动生效

3. **测试 PUA 教练：**
```bash
python tests/test_ai_pua_coach.py
```

**预期结果：**
- ✅ 每次失败都会生成不同的批评
- ✅ 批评内容符合 prompt 中的要求
- ✅ 包含：开场白、失败分析、反思、建议、下一步行动

---

## 🎯 关键改进点

### 1. 搜索词修复
- **问题根源：** Router 生成的 task description 可能是 task_id
- **修复策略：** 在 web_worker 执行前自动检测和修正
- **防御性编程：** 即使上游有问题，下游也能自动修正

### 2. Prompt 可见化
- **问题根源：** Prompt 硬编码在代码中，用户无法查看
- **修复策略：** 提取到独立文件，支持动态加载
- **灵活性：** 用户可以自定义批评风格和要求

### 3. 调试友好
- **添加警告日志：** 当检测到可疑的 task_description 时记录
- **添加调试信息：** 显示实际使用的 task_description
- **便于追踪：** 方便定位问题根源

---

## 🔍 后续优化建议

### 1. 修复 Router 的根本问题
虽然我们在 web_worker 中添加了防御性代码，但最好还是修复 router 生成任务时的问题：
- 检查 router 的 LLM prompt，确保它生成正确的 description
- 在 `build_task_item_from_plan` 中添加验证逻辑
- 确保 description 永远不会是 task_id

### 2. 统一 Prompt 管理
考虑将所有 prompt 都提取到独立文件：
- `prompts/router_system.txt` ✅ 已存在
- `prompts/page_perception.txt` ✅ 已存在
- `prompts/ai_pua_coach.txt` ✅ 新增
- 其他硬编码的 prompt 也可以提取

### 3. 增强 PUA 教练
- 添加成功时的鼓励 prompt（目前只有简单的"可以，这次做对了！"）
- 根据不同类型的失败（网络错误、选择器错误、数据质量问题）使用不同的 prompt
- 添加进度报告的 prompt 模板

---

## ✅ 完成状态

- ✅ 修复搜索词污染问题
- ✅ 提取 PUA 教练 prompt 到独立文件
- ✅ 添加调试日志
- ✅ 添加防御性代码
- ✅ 创建测试方法
- ✅ 编写完整文档

**所有问题已修复，可以立即测试！** 🎉
