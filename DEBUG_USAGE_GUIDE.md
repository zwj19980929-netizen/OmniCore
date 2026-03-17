# 🎉 调试系统使用指南

## 📦 已完成的工作

### 1. 调试输出系统
- ✅ 启用了 `WEB_PERCEPTION_DEBUG=true`
- ✅ 添加了控制台实时输出
- ✅ 自动保存HTML、Prompt、Response到文件

### 2. 有头模式支持
- ✅ 识别"有头"、"显示浏览器"等关键词
- ✅ 自动设置 `headless=False`

### 3. 测试工具
- ✅ `test_headed_vs_headless.sh` - 对比测试脚本
- ✅ `analyze_debug_diff.sh` - 差异分析脚本

## 🚀 快速开始

### 方法1: 使用测试脚本（推荐）

```bash
cd /Users/zhangwenjun/zwj_project/OmniCore
./test_headed_vs_headless.sh
```

这会自动运行两次测试（无头和有头），然后你可以对比结果。

### 方法2: 手动测试

```bash
# 测试无头模式
echo "去 https://github.com/pytorch/pytorch 查看代码结构" | python3 main.py

# 测试有头模式
echo "去 https://github.com/pytorch/pytorch 查看代码结构，有头操作" | python3 main.py
```

### 方法3: 分析已有的调试输出

```bash
./analyze_debug_diff.sh
```

这会自动对比最近两次的调试输出。

## 📊 调试输出位置

所有调试文件保存在：
```
/Users/zhangwenjun/zwj_project/OmniCore/data/debug/web_perception/
```

每次运行会创建一个新目录，格式：
```
20260316_HHMMSS_XXXXXX_web_worker_XXXXXXXX/
```

## 🔍 关键文件说明

| 文件 | 说明 |
|------|------|
| `006_page_raw_html.html` | 原始HTML（最重要） |
| `007_page_cleaned_html.html` | 清洗后的HTML |
| `009_semantic_snapshot.json` | 语义快照（页面类型、元素等） |
| `015_page_analysis_prompt.txt` | 发送给LLM的完整Prompt |
| `017_page_analysis_response.txt` | LLM的完整响应 |
| `021_smart_scrape_result.json` | 最终抓取结果 |

## 💡 如何分析感知差异

### Step 1: 运行对比测试
```bash
./test_headed_vs_headless.sh
```

### Step 2: 查看分析报告
```bash
./analyze_debug_diff.sh
```

### Step 3: 深入对比

如果发现差异，可以手动对比文件：

```bash
# 进入调试目录
cd data/debug/web_perception

# 列出最近的目录
ls -lt | head -3

# 假设最新的两个目录是 DIR1 和 DIR2
DIR1="20260316_224701_..."
DIR2="20260316_224414_..."

# 对比HTML
diff "$DIR1/006_page_raw_html.html" "$DIR2/006_page_raw_html.html" | head -50

# 对比语义快照
diff "$DIR1/009_semantic_snapshot.json" "$DIR2/009_semantic_snapshot.json"

# 对比LLM Prompt
diff "$DIR1/015_page_analysis_prompt.txt" "$DIR2/015_page_analysis_prompt.txt"

# 对比LLM Response
diff "$DIR1/017_page_analysis_response.txt" "$DIR2/017_page_analysis_response.txt"
```

## 🎯 常见问题

### Q1: 为什么没有使用有头模式？

**可能原因**：
1. Router从记忆中找到答案，没有创建新任务
2. 没有使用正确的关键词

**解决方案**：
- 使用新的查询（没有记忆）
- 明确指定URL
- 使用"重新"、"再次"等关键词

### Q2: 如何确认使用了有头模式？

查看控制台输出中的 `browser_pool` 信息：
```
per_key_browser_counts={'chromium:headed': 1}  # 有头模式
per_key_browser_counts={'chromium:headless': 1}  # 无头模式
```

### Q3: 调试文件太多怎么办？

可以定期清理旧的调试文件：
```bash
# 只保留最近3天的调试文件
find data/debug/web_perception -type d -mtime +3 -exec rm -rf {} +
```

## 📚 相关文档

- `QUICKFIX_Debug_Output_Added.md` - 调试输出详细说明
- `QUICKFIX_Debug_And_Headed_Mode.md` - 完整的测试和分析指南
- `docs/Browser_Agent改进报告.md` - 浏览器Agent改进
- `docs/页面信息增强完成报告.md` - 页面信息增强

## 🛠️ 工具脚本

### test_headed_vs_headless.sh
自动运行有头和无头模式的对比测试。

**用法**：
```bash
./test_headed_vs_headless.sh
```

### analyze_debug_diff.sh
自动分析最近两次调试输出的差异。

**用法**：
```bash
./analyze_debug_diff.sh
```

**输出**：
- HTML大小对比
- 语义快照对比
- LLM Prompt大小对比
- LLM响应预览
- 详细对比命令

## 🎉 总结

现在你有了完整的调试系统：

1. **实时输出** - 在控制台看到每步的关键信息
2. **文件记录** - 完整的HTML、Prompt、Response保存到文件
3. **对比工具** - 自动对比有头和无头模式的差异
4. **分析脚本** - 快速找出感知差异的根源

开始测试吧！🚀

```bash
./test_headed_vs_headless.sh
```
