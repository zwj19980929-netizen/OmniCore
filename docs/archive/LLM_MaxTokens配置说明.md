# LLM Max Tokens 配置说明

## 📝 概述

OmniCore 现在支持通过环境变量配置 LLM 的 max_tokens，默认值已提升到 65535，以获得更强大的输出能力。

## 🎯 配置项

### 1. LLM_MAX_TOKENS（默认：65535）
**用途：** 所有 LLM 调用的默认 max_tokens

**推荐值：**
- `65535` - 最大输出能力（推荐）
- `32768` - 平衡性能和成本
- `16000` - 旧版默认值

**适用场景：**
- 所有未明确指定 max_tokens 的 LLM 调用
- 需要长文本输出的场景
- 复杂任务分析

### 2. LLM_ROUTER_MAX_TOKENS（默认：65535）
**用途：** Router 路由分析专用 max_tokens

**推荐值：** `65535`

**适用场景：**
- 用户意图分析
- 任务拆解和规划
- 复杂指令解析

### 3. LLM_CHAT_MAX_TOKENS（默认：32768）
**用途：** 普通对话 max_tokens

**推荐值：** `32768`

**适用场景：**
- 简单对话
- 快速响应场景
- 成本敏感场景

## 📋 配置方法

### 方法 1：通过 .env 文件配置

编辑 `.env` 文件：

```bash
# LLM 调用配置
LLM_MAX_TOKENS=65535
LLM_ROUTER_MAX_TOKENS=65535
LLM_CHAT_MAX_TOKENS=32768
```

### 方法 2：通过环境变量配置

```bash
export LLM_MAX_TOKENS=65535
export LLM_ROUTER_MAX_TOKENS=65535
export LLM_CHAT_MAX_TOKENS=32768
```

### 方法 3：启动时指定

```bash
LLM_MAX_TOKENS=65535 python main.py
```

## 🔧 代码中的使用

### 自动使用配置的默认值

```python
from core.llm import LLMClient

llm = LLMClient()

# 使用配置的 LLM_MAX_TOKENS（65535）
response = llm.chat(messages)

# 使用配置的 LLM_MAX_TOKENS（65535）
response = llm.chat_with_system(
    system_prompt="你是一个助手",
    user_message="你好"
)
```

### 手动指定 max_tokens

```python
# 覆盖默认值，使用自定义的 max_tokens
response = llm.chat(messages, max_tokens=8192)

response = llm.chat_with_system(
    system_prompt="你是一个助手",
    user_message="你好",
    max_tokens=8192
)
```

## ⚠️ 注意事项

### 1. 模型限制
不同模型有不同的 max_tokens 限制：
- GPT-4: 最大 128k tokens（输入+输出）
- Claude 3: 最大 200k tokens（输入+输出）
- Gemini Pro: 最大 32k tokens（输出）
- DeepSeek: 最大 4k tokens（输出）

系统会自动调整到模型支持的最大值。

### 2. 成本考虑
更大的 max_tokens 意味着：
- ✅ 更强的输出能力
- ✅ 更完整的响应
- ❌ 更高的 API 成本
- ❌ 更长的响应时间

### 3. 自动降级
如果设置的 max_tokens 超过模型限制，系统会：
1. 自动检测模型的 max_tokens 限制
2. 降级到模型支持的最大值
3. 记录警告日志
4. 继续执行

## 📊 默认值对比

| 配置项 | 旧版默认值 | 新版默认值 | 提升 |
|--------|-----------|-----------|------|
| LLM_MAX_TOKENS | 16000 | 65535 | 4.1x |
| LLM_ROUTER_MAX_TOKENS | 16000 | 65535 | 4.1x |
| LLM_CHAT_MAX_TOKENS | - | 32768 | 新增 |

## 🎯 推荐配置

### 开发环境（追求能力）
```bash
LLM_MAX_TOKENS=65535
LLM_ROUTER_MAX_TOKENS=65535
LLM_CHAT_MAX_TOKENS=32768
```

### 生产环境（平衡成本）
```bash
LLM_MAX_TOKENS=32768
LLM_ROUTER_MAX_TOKENS=65535
LLM_CHAT_MAX_TOKENS=16000
```

### 成本敏感环境
```bash
LLM_MAX_TOKENS=16000
LLM_ROUTER_MAX_TOKENS=32768
LLM_CHAT_MAX_TOKENS=8192
```

## 🔍 调试

### 查看实际使用的 max_tokens

启用 DEBUG 模式：
```bash
DEBUG_MODE=true python main.py
```

日志会显示：
```
[INFO] LLM 调用开始: model=gpt-4o, max_tokens=65535, timeout=120s
```

### 查看配置值

```python
from config.settings import settings

print(f"LLM_MAX_TOKENS: {settings.LLM_MAX_TOKENS}")
print(f"LLM_ROUTER_MAX_TOKENS: {settings.LLM_ROUTER_MAX_TOKENS}")
print(f"LLM_CHAT_MAX_TOKENS: {settings.LLM_CHAT_MAX_TOKENS}")
```

## 📝 修改的文件

1. **config/settings.py**
   - 添加 `LLM_MAX_TOKENS`（默认 65535）
   - 添加 `LLM_ROUTER_MAX_TOKENS`（默认 65535）
   - 添加 `LLM_CHAT_MAX_TOKENS`（默认 32768）

2. **core/llm.py**
   - 修改 `chat()` 方法，max_tokens 默认值改为 None
   - 修改 `achat()` 方法，max_tokens 默认值改为 None
   - 修改 `chat_with_system()` 方法，max_tokens 默认值改为 None
   - 当 max_tokens=None 时，自动使用 `settings.LLM_MAX_TOKENS`

3. **core/router.py**
   - Router 使用 `settings.LLM_ROUTER_MAX_TOKENS`

4. **.env.example**
   - 添加 LLM max_tokens 配置说明

## ✅ 完成

现在所有 LLM 调用默认使用 65535 max_tokens，你可以通过环境变量灵活配置！
