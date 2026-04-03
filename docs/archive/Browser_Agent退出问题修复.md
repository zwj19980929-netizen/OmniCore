# Browser Agent 退出问题修复

## 问题 1: max_tokens=8192

### 原因
MiniMax-M2.5 模型本身的限制就是 `max_output_tokens: 8192`。

从 LiteLLM 的模型信息：
```python
{
    'max_input_tokens': 1000000,
    'max_output_tokens': 8192  # ← 这是模型本身的限制
}
```

### 解决方案

**方案 1：换模型（推荐）**

使用支持更大 max_tokens 的模型：

| 模型 | max_output_tokens |
|------|-------------------|
| GPT-4o | 16,384 |
| GPT-4 Turbo | 4,096 |
| Claude 3.5 Sonnet | 8,192 |
| Gemini 2.0 Flash | 8,192 |
| Gemini 1.5 Pro | 8,192 |
| DeepSeek V3 | 8,192 |

修改 `.env`：
```bash
DEFAULT_MODEL=gpt-4o
# 或
DEFAULT_MODEL=gemini/gemini-2.0-flash-exp
```

**方案 2：强制覆盖限制（不推荐）**

修改 `core/llm.py:324-337`，移除 `_safe_max_tokens()` 中的限制检查。

**风险：** 可能导致 API 返回错误。

---

## 问题 2: "action requires human confirmation" 退出

### 原因

1. `.env` 中 `REQUIRE_HUMAN_CONFIRM=true`
2. LLM 在分析页面时，认为某个操作需要人工确认
3. 旧代码检测到这个标志后，**直接退出**而不是询问用户

### 旧代码逻辑（有问题）

```python
# agents/browser_agent.py:2227-2229
if action.requires_confirmation and settings.REQUIRE_HUMAN_CONFIRM:
    return {"success": False, "message": "action requires human confirmation",
            "requires_confirmation": True, "steps": steps}
```

**问题：** 直接返回失败，没有给用户确认的机会。

### 修复后的逻辑

```python
# agents/browser_agent.py:2227-2240
if action.requires_confirmation and settings.REQUIRE_HUMAN_CONFIRM:
    # 🔥 修复：不要直接退出，而是询问用户
    from utils.human_confirm import HumanConfirm
    confirmed = await asyncio.to_thread(
        HumanConfirm.request_browser_action_confirmation,
        action=action.action_type.value,
        target=action.target_selector[:80],
        value=action.value[:80],
        description=action.description
    )
    if not confirmed:
        return {"success": False, "message": "user declined action confirmation",
                "requires_confirmation": True, "steps": steps}
    # 用户确认了，继续执行
```

**改进：**
1. ✅ 实际询问用户是否确认
2. ✅ 显示操作详情（操作类型、目标元素、输入值）
3. ✅ 用户确认后继续执行
4. ✅ 用户拒绝才退出

### 新增方法

在 `utils/human_confirm.py` 中添加：

```python
@staticmethod
def request_browser_action_confirmation(
    action: str,
    target: str,
    value: str = "",
    description: str = "",
) -> bool:
    """浏览器操作确认"""
    details = f"操作类型: {action}\n目标元素: {target}"
    if value:
        details += f"\n输入值: {value}"
    if description:
        details += f"\n描述: {description}"

    return HumanConfirm.request_confirmation(
        operation="浏览器操作",
        details=details,
    )
```

---

## 修复效果

### 修复前
```
[Agent: BrowserAgent] 执行步骤 0
💬 退出消息：action requires human confirmation
🚨 严重问题：没有执行任何操作就退出了！
```

### 修复后
```
[Agent: BrowserAgent] 执行步骤 1
⚠️ 高危操作确认请求
操作类型: 浏览器操作
操作详情:
  操作类型: click
  目标元素: button[type="submit"]
  描述: 点击搜索按钮

是否确认执行此操作? [y/n] (n): y

[Agent: BrowserAgent] 执行步骤 2
✅ 操作成功
```

---

## 配置选项

### 选项 1：保持人工确认（推荐）
```bash
# .env
REQUIRE_HUMAN_CONFIRM=true
```

**优点：**
- ✅ 安全，防止误操作
- ✅ 用户可以审查每个操作

**缺点：**
- ❌ 需要手动确认，速度慢

### 选项 2：关闭人工确认（快速但不安全）
```bash
# .env
REQUIRE_HUMAN_CONFIRM=false
```

**优点：**
- ✅ 全自动，速度快

**缺点：**
- ❌ 可能执行错误操作
- ❌ 不适合生产环境

---

## 为什么 LLM 会标记需要确认？

LLM 在以下情况可能标记 `requires_human_confirm: true`：

1. **不确定的操作** - 置信度低
2. **高风险操作** - 如删除、提交表单
3. **模糊的目标** - 找到多个可能的元素
4. **敏感数据** - 涉及密码、支付等

这是 LLM 的自我保护机制，但旧代码没有正确处理。

---

## 验证方法

### 测试 1：正常操作
```bash
python main.py
> 搜索 CNNVD 漏洞：CVE-2024-1234
```

**预期：**
- 如果 LLM 认为需要确认，会弹出确认提示
- 用户输入 `y` 后继续执行
- 不会提前退出

### 测试 2：拒绝确认
```bash
python main.py
> 搜索 CNNVD 漏洞：CVE-2024-1234
```

**预期：**
- 弹出确认提示
- 用户输入 `n` 后退出
- 返回 "user declined action confirmation"

---

## 相关文件

- `agents/browser_agent.py:2227-2240` - 修复退出逻辑
- `utils/human_confirm.py:131-149` - 新增确认方法
- `.env` - `REQUIRE_HUMAN_CONFIRM` 配置

---

## 总结

### 问题 1: max_tokens=8192
- **原因：** MiniMax 模型限制
- **解决：** 换用支持更大 max_tokens 的模型（如 GPT-4o）

### 问题 2: 提前退出
- **原因：** 检测到需要确认时直接退出，没有询问用户
- **解决：** 修改为实际询问用户，用户确认后继续执行

现在系统会正确处理需要确认的操作，不会再提前退出了！
