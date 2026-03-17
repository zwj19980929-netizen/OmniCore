#!/bin/bash
# 测试有头和无头模式的差异

echo "=========================================="
echo "测试1: 无头模式（默认）"
echo "=========================================="
echo "去 https://github.com/pytorch/pytorch 查看代码结构" | python3 main.py
echo ""
echo "调试文件保存在上面显示的路径"
echo ""
read -p "按回车继续测试有头模式..."

echo ""
echo "=========================================="
echo "测试2: 有头模式"
echo "=========================================="
echo "去 https://github.com/pytorch/pytorch 查看代码结构，有头操作" | python3 main.py
echo ""
echo "调试文件保存在上面显示的路径"
echo ""

echo "=========================================="
echo "测试完成！"
echo "=========================================="
echo "现在你可以对比两次的调试文件："
echo "1. 查看 data/debug/web_perception/ 目录"
echo "2. 找到最新的两个目录"
echo "3. 对比它们的文件内容"
echo ""
echo "关键文件："
echo "  - 006_page_raw_html.html (原始HTML)"
echo "  - 009_semantic_snapshot.json (语义快照)"
echo "  - 015_page_analysis_prompt.txt (LLM Prompt)"
echo "  - 017_page_analysis_response.txt (LLM Response)"
