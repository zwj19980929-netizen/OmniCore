# 移除硬编码 max_tokens 修复说明

## 问题描述

用户发现日志中显示 `max_tokens=2048`，但期望使用配置的默认值 65535。

问题原因：虽然在 `core/llm.py` 中设置了默认的 `max_tokens=None` 并从 settings 读取 65535，但是很多地方都显式传入了硬编码的 `max_tokens` 值，这些值会覆盖默认配置。

## 修复内容

### 已移除硬编码 max_tokens 的文件

1. **agents/enhanced_web_worker.py**
   - 第 134 行：`max_tokens=2048` → 移除（页面理解）
   - 第 175 行：`max_tokens=3072` → 移除（选择器生成）
   - 第 312 行：`max_tokens=1536` → 移除（操作规划）

2. **agents/web_worker.py**
   - 第 1371 行：`max_tokens=4096` → 移除（页面分析）

3. **agents/web_worker_singleflight.py**
   - 第 227 行：`max_tokens=4096` → 移除（页面分析）

4. **agents/browser_agent.py**
   - 第 699 行：`max_tokens=1200` → 移除（动作决策）

5. **core/graph.py**
   - 第 1433 行：`max_tokens=1200` → 移除（结果合成）
   - 第 1767 行：`max_tokens=1200` → 移除（结果合成）

6. **utils/captcha_solver.py**
   - 第 140 行：`max_tokens=500` → 移除（验证码识别）

7. **utils/ai_pua_coach.py**
   - 第 185 行：`max_tokens=2048` → 移除（PUA 评价生成）
   - 第 260 行：`max_tokens=512` → 移除（进度报告）

8. **memory/entity_extractor.py**
   - 第 69 行：`max_tokens=1000` → 移除（实体提取）

### 保留的硬编码值

**core/router.py**
- 第 330 行：`max_tokens=800` → 改为 `max_tokens=2048`（事实验证守卫）
- 保留原因：这是一个特定的小任务，不需要使用默认的 65535

## 效果

现在所有 LLM 调用都会使用配置的默认值：
- 默认：`LLM_MAX_TOKENS=65535`
- Router：`LLM_ROUTER_MAX_TOKENS=65535`
- 普通对话：`LLM_CHAT_MAX_TOKENS=32768`

除非特定场景需要限制（如事实验证守卫的 2048），否则都会使用这些配置值。

## 配置方法

在 `.env` 文件中配置：

```bash
# LLM 调用配置
LLM_MAX_TOKENS=65535
LLM_ROUTER_MAX_TOKENS=65535
LLM_CHAT_MAX_TOKENS=32768
```

## 验证

运行系统后，日志应该显示：
```
[INFO] LLM 调用开始: model=xxx, max_tokens=65535, timeout=120s
```

而不是之前的 `max_tokens=2048` 或其他小值。
