#!/bin/bash
# 快速分析调试输出的差异

DEBUG_DIR="/Users/zhangwenjun/zwj_project/OmniCore/data/debug/web_perception"

echo "=========================================="
echo "调试文件分析工具"
echo "=========================================="
echo ""

# 列出最近的两个调试目录
echo "最近的调试会话："
ls -lt "$DEBUG_DIR" | grep "^d" | head -2 | awk '{print NR". "$9}'
echo ""

# 获取最近的两个目录
DIR1=$(ls -t "$DEBUG_DIR" | head -1)
DIR2=$(ls -t "$DEBUG_DIR" | head -2 | tail -1)

if [ -z "$DIR1" ] || [ -z "$DIR2" ]; then
    echo "错误：找不到足够的调试目录"
    echo "请先运行测试脚本: ./test_headed_vs_headless.sh"
    exit 1
fi

echo "对比目录："
echo "  新: $DIR1"
echo "  旧: $DIR2"
echo ""

# 对比HTML大小
echo "=========================================="
echo "1. HTML大小对比"
echo "=========================================="
HTML1="$DEBUG_DIR/$DIR1/006_page_raw_html.html"
HTML2="$DEBUG_DIR/$DIR2/006_page_raw_html.html"

if [ -f "$HTML1" ] && [ -f "$HTML2" ]; then
    SIZE1=$(wc -c < "$HTML1")
    SIZE2=$(wc -c < "$HTML2")
    echo "新: $SIZE1 字节"
    echo "旧: $SIZE2 字节"
    echo "差异: $((SIZE1 - SIZE2)) 字节"
else
    echo "HTML文件不存在"
fi
echo ""

# 对比语义快照
echo "=========================================="
echo "2. 语义快照对比"
echo "=========================================="
SNAP1="$DEBUG_DIR/$DIR1/009_semantic_snapshot.json"
SNAP2="$DEBUG_DIR/$DIR2/009_semantic_snapshot.json"

if [ -f "$SNAP1" ] && [ -f "$SNAP2" ]; then
    echo "新的页面类型: $(jq -r '.page_type // "unknown"' "$SNAP1")"
    echo "旧的页面类型: $(jq -r '.page_type // "unknown"' "$SNAP2")"
    echo ""
    echo "新的元素数量: $(jq '.elements | length' "$SNAP1")"
    echo "旧的元素数量: $(jq '.elements | length' "$SNAP2")"
else
    echo "语义快照文件不存在"
fi
echo ""

# 对比Prompt大小
echo "=========================================="
echo "3. LLM Prompt大小对比"
echo "=========================================="
PROMPT1="$DEBUG_DIR/$DIR1/015_page_analysis_prompt.txt"
PROMPT2="$DEBUG_DIR/$DIR2/015_page_analysis_prompt.txt"

if [ -f "$PROMPT1" ] && [ -f "$PROMPT2" ]; then
    SIZE1=$(wc -c < "$PROMPT1")
    SIZE2=$(wc -c < "$PROMPT2")
    echo "新: $SIZE1 字节"
    echo "旧: $SIZE2 字节"
    echo "差异: $((SIZE1 - SIZE2)) 字节"
else
    echo "Prompt文件不存在"
fi
echo ""

# 显示LLM响应
echo "=========================================="
echo "4. LLM响应预览"
echo "=========================================="
RESP1="$DEBUG_DIR/$DIR1/017_page_analysis_response.txt"
RESP2="$DEBUG_DIR/$DIR2/017_page_analysis_response.txt"

if [ -f "$RESP1" ]; then
    echo "新的响应 (前300字符):"
    head -c 300 "$RESP1"
    echo ""
    echo "..."
    echo ""
fi

if [ -f "$RESP2" ]; then
    echo "旧的响应 (前300字符):"
    head -c 300 "$RESP2"
    echo ""
    echo "..."
fi
echo ""

echo "=========================================="
echo "详细对比"
echo "=========================================="
echo "要查看完整的文件差异，运行："
echo ""
echo "  # 对比HTML"
echo "  diff \"$HTML1\" \"$HTML2\" | head -50"
echo ""
echo "  # 对比语义快照"
echo "  diff \"$SNAP1\" \"$SNAP2\""
echo ""
echo "  # 对比Prompt"
echo "  diff \"$PROMPT1\" \"$PROMPT2\""
echo ""
echo "  # 对比Response"
echo "  diff \"$RESP1\" \"$RESP2\""
echo ""
