#!/bin/bash
# 全阶段修复验证脚本

echo "=========================================="
echo "全阶段修复验证 - 网页感知增强"
echo "=========================================="
echo ""

# 检查虚拟环境
if [ ! -d ".venv" ]; then
    echo "❌ 错误：未找到虚拟环境 .venv"
    echo "请先运行: python -m venv .venv"
    exit 1
fi

# 激活虚拟环境
echo "✓ 激活虚拟环境..."
source .venv/bin/activate

echo ""
echo "=========================================="
echo "选择测试模式："
echo "=========================================="
echo "1. 快速验证 (推荐) - 测试所有阶段的基础功能"
echo "2. 阶段1完整测试 - web_worker 页面感知"
echo "3. 阶段2和阶段3验证 - browser_agent + EnhancedWebWorker"
echo "4. 全部测试 - 运行所有测试"
echo ""
read -p "请选择 (1/2/3/4): " choice

case $choice in
    1)
        echo ""
        echo "运行快速验证..."
        echo ""
        echo "=== 测试 PagePerceiver ==="
        python tests/test_perceiver_quick.py
        echo ""
        echo "=== 测试阶段2和阶段3 ==="
        python tests/test_stage2_stage3.py
        ;;
    2)
        echo ""
        echo "运行阶段1完整测试..."
        python tests/test_web_worker_perception.py
        ;;
    3)
        echo ""
        echo "运行阶段2和阶段3验证..."
        python tests/test_stage2_stage3.py
        ;;
    4)
        echo ""
        echo "运行全部测试..."
        echo ""
        echo "=== 阶段1: PagePerceiver 快速验证 ==="
        python tests/test_perceiver_quick.py
        echo ""
        echo "=== 阶段1: web_worker 完整测试 ==="
        python tests/test_web_worker_perception.py
        echo ""
        echo "=== 阶段2和阶段3验证 ==="
        python tests/test_stage2_stage3.py
        ;;
    *)
        echo "无效选择"
        exit 1
        ;;
esac

echo ""
echo "=========================================="
echo "测试完成"
echo "=========================================="
echo ""
echo "📖 查看详细文档："
echo "  - ALL_STAGES_COMPLETE.md - 完整修复总结"
echo "  - STAGE1_FIXED.md - 阶段1详细文档"
echo "  - FIX_PLAN.md - 原始修复计划"
echo ""
echo "🎉 所有三个阶段的修复已完成！"
