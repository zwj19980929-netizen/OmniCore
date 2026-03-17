# 🔍 调试输出已添加

## ✅ 已完成的改进

为了帮助你理解"为什么有头模式感知差，无头模式感知好"，我添加了全面的调试输出。

### 1. 启用调试模式

**文件**: `.env`

```bash
# 启用网页感知调试（会输出HTML、LLM prompt和response到文件）
WEB_PERCEPTION_DEBUG=true
```

### 2. 添加的调试输出

**文件**: `agents/browser_agent.py`

#### 2.1 调试文件位置提示
```
[DEBUG] 调试文件保存在: /path/to/debug/folder
[DEBUG] 你可以查看该目录下的HTML、prompt和response文件来分析感知差异
```

#### 2.2 初始导航信息
```
[DEBUG] ========== 初始导航完成 ==========
[DEBUG] 目标URL: https://github.com/...
[DEBUG] 当前URL: https://github.com/...
[DEBUG] 页面标题: GitHub - ...
[DEBUG] ====================================
```

#### 2.3 语义快照信息
```
[DEBUG] ========== 语义快照 ==========
[DEBUG] 页面类型: repository
[DEBUG] 页面阶段: ready
[DEBUG] 元素数量: 45
[DEBUG] 卡片数量: 0
[DEBUG] 集合数量: 2
[DEBUG] 主要文本 (前200字符): OpenClaw is a...
[DEBUG] ====================================
```

#### 2.4 每步执行信息
```
[DEBUG] ========== Step 1 开始 ==========
[DEBUG] 当前URL: https://github.com/...
[DEBUG] 页面标题: GitHub - ...
[DEBUG] 可交互元素数量: 45
[DEBUG] 已收集数据: 0 条
[DEBUG] ====================================
```

#### 2.5 页面HTML信息
```
[DEBUG] 页面HTML (前1000字符): <!DOCTYPE html><html>...
[DEBUG] 页面HTML总长度: 125000 字符
```

#### 2.6 LLM Prompt信息
```
[DEBUG] 页面评估 Prompt (前800字符): You are analyzing...
[DEBUG] 页面评估 Prompt总长度: 3500 字符
[DEBUG] 元素数量: 45, 数据条数: 0
```

#### 2.7 LLM Response信息
```
[DEBUG] 页面评估 LLM 响应: {"action": "extract", "reasoning": "..."}...
[DEBUG] 页面评估 payload: {"action": "extract", "confidence": 0.9}...
```

#### 2.8 决策动作信息
```
[DEBUG] Step 1 决策动作: extract
[DEBUG] 动作描述: extract repository structure
[DEBUG] 目标选择器: div.repository-content
[DEBUG] 置信度: 0.9
```

#### 2.9 动作决策信息
```
[DEBUG] 动作决策 Prompt (前800字符): Decide next action...
[DEBUG] 动作决策 Prompt总长度: 4200 字符
[DEBUG] 动作决策 LLM 响应: {"action": "click", "target": "..."}...
```

## 📊 调试文件结构

当你运行任务时，会在以下目录生成调试文件：

```
data/debug/web_perception/
└── 20260316_223145_123456_browser_agent_abc123/
    ├── 001_manifest.json                          # 任务元信息
    ├── 002_browser_initial_navigation.json        # 初始导航
    ├── 003_browser_semantic_snapshot.json         # 语义快照
    ├── 004_browser_page_html.html                 # 页面HTML
    ├── 005_browser_step_1_context.json            # Step 1 上下文
    ├── 006_browser_page_assessment_context.json   # 页面评估上下文
    ├── 007_browser_page_assessment_prompt.txt     # 页面评估 Prompt
    ├── 008_browser_page_assessment_budget.json    # Prompt预算分配
    ├── 009_browser_page_assessment_response.txt   # LLM响应
    ├── 010_browser_page_assessment_payload.json   # 解析后的payload
    ├── 011_browser_page_assessment_action.json    # 决策的动作
    ├── 012_browser_step_1_action.json             # Step 1 最终动作
    ├── 013_browser_step_1_result.json             # Step 1 执行结果
    └── ...
```

## 🎯 如何使用

### 1. 运行测试
```bash
python main.py

# 输入：
> 去github给我找找openclaw的代码路径，有头操作
```

### 2. 观察控制台输出
- 实时查看每步的URL、标题、元素数量
- 查看LLM收到的prompt和返回的response
- 查看页面HTML的长度和内容预览
- 查看语义快照的结构

### 3. 查看调试文件
- 控制台会显示调试文件保存路径
- 打开该目录，查看完整的HTML、prompt、response
- 对比有头和无头模式的差异

## 🔍 分析感知差异的步骤

### Step 1: 对比HTML
```bash
# 有头模式
cat data/debug/web_perception/xxx_headed/004_browser_page_html.html

# 无头模式
cat data/debug/web_perception/xxx_headless/004_browser_page_html.html
```

**问题检查**：
- HTML长度是否一致？
- 关键元素是否存在？
- 是否有反爬虫检测导致的差异？

### Step 2: 对比语义快照
```bash
# 有头模式
cat data/debug/web_perception/xxx_headed/003_browser_semantic_snapshot.json

# 无头模式
cat data/debug/web_perception/xxx_headless/003_browser_semantic_snapshot.json
```

**问题检查**：
- page_type 是否一致？
- 元素数量是否一致？
- cards/collections 是否一致？
- main_text 是否一致？

### Step 3: 对比LLM Prompt
```bash
# 有头模式
cat data/debug/web_perception/xxx_headed/007_browser_page_assessment_prompt.txt

# 无头模式
cat data/debug/web_perception/xxx_headless/007_browser_page_assessment_prompt.txt
```

**问题检查**：
- LLM收到的信息是否一致？
- 元素列表是否一致？
- 上下文信息是否一致？

### Step 4: 对比LLM Response
```bash
# 有头模式
cat data/debug/web_perception/xxx_headed/009_browser_page_assessment_response.txt

# 无头模式
cat data/debug/web_perception/xxx_headless/009_browser_page_assessment_response.txt
```

**问题检查**：
- LLM的推理是否一致？
- 决策的动作是否一致？
- 置信度是否一致？

## 💡 预期发现

根据你的描述"无头感知好，有头感知差"，可能的原因：

### 可能性1: HTML差异
- 有头模式触发了反爬虫检测
- 页面加载了不同的内容
- JavaScript执行结果不同

### 可能性2: 语义快照差异
- 有头模式提取的元素不准确
- page_type 判断错误
- 关键信息丢失

### 可能性3: LLM Prompt差异
- 有头模式的prompt缺少关键信息
- 元素列表不完整
- 上下文信息混乱

### 可能性4: LLM推理差异
- 相同的prompt，LLM给出不同的推理
- 可能是随机性导致的（temperature=0.1仍有随机性）

## 🚀 下一步

1. **运行测试**：先运行无头模式，再运行有头模式
2. **对比文件**：使用上述步骤对比调试文件
3. **定位问题**：找出哪个环节出现了差异
4. **针对性修复**：根据差异点进行修复

---

**修改的文件**：
1. `.env` - 启用调试模式
2. `agents/browser_agent.py` - 添加调试输出

**测试建议**：
重新运行之前失败的任务，观察控制台输出和调试文件！
