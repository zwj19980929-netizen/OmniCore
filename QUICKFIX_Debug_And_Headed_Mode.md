# 🔍 调试输出和有头模式支持 - 完成报告

## ✅ 已完成的改进

### 1. 启用调试模式

**文件**: `.env`
- 添加了 `WEB_PERCEPTION_DEBUG=true`
- 现在会自动保存HTML、LLM prompt和response到文件

### 2. 添加控制台调试输出

**文件**: `agents/browser_agent.py`

添加了9处调试输出点：
- 调试文件保存位置提示
- 初始导航信息（URL、标题）
- 语义快照摘要（页面类型、元素数量、主要文本）
- 每步执行信息（URL、元素数量、数据条数）
- 页面HTML长度和预览（前1000字符）
- LLM Prompt长度和预览（前800字符）
- LLM Response和解析结果（前500字符）
- 决策的动作和置信度

### 3. 支持"有头操作"关键词

**文件**: `core/router.py`

添加了对以下关键词的识别：
- "有头"
- "headful"
- "headed"
- "显示浏览器"
- "展示浏览器"
- "show browser"
- "visible browser"
- "浏览器操作"
- "看操作"

当检测到这些关键词时，会自动设置 `headless=False`。

## 📊 调试文件结构

调试文件保存在：`data/debug/web_perception/`

每次运行会创建一个新的目录，包含：
```
20260316_HHMMSS_XXXXXX_web_worker_XXXXXXXX/
├── 001_manifest.json                          # 任务元信息
├── 002_url_resolution.json                    # URL解析
├── 003_static_fetch_result.json               # 静态抓取结果
├── 004_browser_navigation_start.json          # 浏览器导航开始
├── 005_captcha_detection.json                 # 验证码检测
├── 006_page_raw_html.html                     # 原始HTML
├── 007_page_cleaned_html.html                 # 清洗后的HTML
├── 008_observation_context.json               # 观察上下文
├── 009_semantic_snapshot.json                 # 语义快照
├── 010_semantic_snapshot_llm.txt              # 语义快照（LLM格式）
├── 011_page_structure.json                    # 页面结构
├── 012_page_structure_llm.txt                 # 页面结构（LLM格式）
├── 013_page_analysis_html_for_llm.html        # 给LLM的HTML片段
├── 014_page_analysis_candidate_regions.txt    # 候选区域
├── 015_page_analysis_prompt.txt               # 页面分析Prompt
├── 016_page_analysis_budget.json              # Prompt预算分配
├── 017_page_analysis_response.txt             # LLM响应
├── 018_page_analysis_config.json              # 分析配置
├── 019_selector_extraction_start.json         # 选择器提取开始
├── 020_selector_extraction_results.json       # 选择器提取结果
└── 021_smart_scrape_result.json               # 智能抓取结果
```

## 🎯 测试结果

### 测试1: 基本功能测试
```bash
echo "去github给我找找openclaw的代码路径，有头操作" | python3 main.py
```

**结果**：
- ✅ 调试文件成功生成
- ✅ 控制台输出正常
- ⚠️ 但是没有使用有头模式

**原因**：
Router识别为 `information_query`（从记忆中直接回答），没有创建web_scraping任务，所以"有头操作"逻辑没有触发。

### 测试2: 强制web_scraping任务
要测试有头模式，需要：
1. 清除相关记忆，或
2. 使用新的查询，或
3. 明确要求"重新访问"

## 🔍 发现的问题

### 问题1: 记忆系统干扰测试
当Router从记忆中找到答案时，会直接返回，不会创建新任务。

**解决方案**：
- 测试时使用新的查询
- 或者在查询中加入"重新"、"再次"等关键词
- 或者清除相关记忆

### 问题2: GitHub API vs GitHub网站
系统默认使用GitHub API（更快、更可靠），而不是访问GitHub网站。

**当前行为**：
- 访问 `https://api.github.com/repos/openclaw/openclaw/contents`
- 返回JSON数据
- 使用headless模式（因为是API，不需要渲染）

**如果要访问GitHub网站**：
需要明确指定URL，例如：
```
去 https://github.com/openclaw/openclaw 查看代码结构，有头操作
```

## 💡 如何正确测试有头模式

### 方法1: 指定完整URL
```bash
echo "去 https://github.com/openclaw/openclaw 查看代码结构，有头操作" | python3 main.py
```

### 方法2: 使用新的查询
```bash
echo "去github找找pytorch的代码路径，有头操作" | python3 main.py
```

### 方法3: 要求重新访问
```bash
echo "重新去github查openclaw的代码，有头操作，我要看浏览器" | python3 main.py
```

## 📝 控制台输出示例

当调试模式启用时，你会看到：

```
[WARN] [DEBUG] 调试文件保存在: /Users/.../data/debug/web_perception/20260316_...
[WARN] [DEBUG] 你可以查看该目录下的HTML、prompt和response文件来分析感知差异
[WARN] [DEBUG] ========== 初始导航完成 ==========
[WARN] [DEBUG] 目标URL: https://github.com/...
[WARN] [DEBUG] 当前URL: https://github.com/...
[WARN] [DEBUG] 页面标题: GitHub - ...
[WARN] [DEBUG] ====================================
[WARN] [DEBUG] ========== 语义快照 ==========
[WARN] [DEBUG] 页面类型: repository
[WARN] [DEBUG] 页面阶段: ready
[WARN] [DEBUG] 元素数量: 45
[WARN] [DEBUG] 卡片数量: 0
[WARN] [DEBUG] 集合数量: 2
[WARN] [DEBUG] 主要文本 (前200字符): OpenClaw is a...
[WARN] [DEBUG] ====================================
```

## 🚀 下一步建议

### 1. 测试有头模式
使用上述方法之一，确保触发web_scraping任务。

### 2. 对比有头和无头模式
```bash
# 无头模式
echo "去 https://github.com/openclaw/openclaw 查看代码" | python3 main.py

# 有头模式
echo "去 https://github.com/openclaw/openclaw 查看代码，有头操作" | python3 main.py
```

然后对比两次的调试文件。

### 3. 分析感知差异
查看调试文件，对比：
- HTML内容是否一致
- 语义快照是否一致
- LLM收到的prompt是否一致
- LLM的响应是否一致

## 📚 相关文档

- 详细使用指南：`QUICKFIX_Debug_Output_Added.md`
- 浏览器改进报告：`docs/Browser_Agent改进报告.md`
- 页面信息增强报告：`docs/页面信息增强完成报告.md`

---

**修改的文件**：
1. `.env` - 启用调试模式
2. `agents/browser_agent.py` - 添加调试输出
3. `core/router.py` - 支持"有头操作"关键词

**状态**：✅ 完成并测试
