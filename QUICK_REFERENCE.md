# 🎯 调试系统快速参考

## 一键测试

```bash
# 测试有头模式
echo "去 https://github.com/pytorch/pytorch 查看代码，有头操作" | python3 main.py

# 对比测试（有头 vs 无头）
./test_headed_vs_headless.sh

# 分析差异
./analyze_debug_diff.sh
```

## 关键词

**触发有头模式**：
- 有头、headful、headed
- 显示浏览器、展示浏览器
- show browser、visible browser
- 浏览器操作、看操作

**触发重新执行**：
- 重新、再次、again、重做

## 调试文件位置

```
data/debug/web_perception/20260316_HHMMSS_*/
├── 006_page_raw_html.html          # 原始HTML
├── 009_semantic_snapshot.json      # 语义快照
├── 015_page_analysis_prompt.txt    # LLM Prompt
└── 017_page_analysis_response.txt  # LLM Response
```

## 验证有头模式

```bash
# 查看日志
grep "chromium:" /tmp/omnicore_debug.log

# 应该看到
chromium:headed  ✅ 有头模式
chromium:headless  ❌ 无头模式
```

## 常见问题

**Q: 为什么没有使用有头模式？**
A: 检查是否使用了正确的关键词，或者查看intent是否为web_scraping

**Q: 如何对比有头和无头的差异？**
A: 运行 `./test_headed_vs_headless.sh` 然后 `./analyze_debug_diff.sh`

**Q: 调试文件在哪里？**
A: 控制台会显示路径，或查看 `data/debug/web_perception/` 最新目录

## 修复的问题

✅ "有头操作"现在正确触发有头模式
✅ 自动将API URL转换为网页URL
✅ 即使有记忆也会重新执行
✅ Confidence字符串解析错误已修复
✅ 完整的调试输出（HTML、Prompt、Response）

## 文档

- `QUICKFIX_Headed_Mode_Fixed.md` - 详细修复报告
- `DEBUG_USAGE_GUIDE.md` - 完整使用指南
- `QUICKFIX_Debug_Output_Added.md` - 调试输出说明
