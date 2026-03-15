#!/bin/bash
# 阶段1修复验证脚本

echo "=========================================="
echo "阶段1修复验证 - WebWorker 页面感知"
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

# 检查依赖
echo "✓ 检查依赖..."
python -c "from utils.page_perceiver import PagePerceiver" 2>/dev/null
if [ $? -ne 0 ]; then
    echo "❌ 错误：PagePerceiver 导入失败"
    exit 1
fi

echo ""
echo "=========================================="
echo "选择测试模式："
echo "=========================================="
echo "1. 快速验证 (推荐) - 只测试 PagePerceiver"
echo "2. 完整测试 - 测试 WebWorker 集成"
echo "3. 真实场景 - 运行实际任务"
echo ""
read -p "请选择 (1/2/3): " choice

case $choice in
    1)
        echo ""
        echo "运行快速验证..."
        python tests/test_perceiver_quick.py
        ;;
    2)
        echo ""
        echo "运行完整测试..."
        python tests/test_web_worker_perception.py
        ;;
    3)
        echo ""
        echo "运行真实场景测试..."
        python main.py "去 Hacker News 抓取前 10 条新闻标题和链接"
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
