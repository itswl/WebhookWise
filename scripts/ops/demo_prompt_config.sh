#!/bin/bash
# AI Prompt 配置演示脚本

set -e

echo "=========================================="
echo "AI Prompt 动态配置 - 演示"
echo "=========================================="

BASE_URL="http://localhost:5000"

echo ""
echo "1️⃣  检查服务状态"
if curl -s "$BASE_URL/health" > /dev/null 2>&1; then
    echo "   ✅ 服务运行正常"
else
    echo "   ❌ 服务未运行，请先启动: python app.py"
    exit 1
fi

echo ""
echo "2️⃣  查看当前 Prompt 配置"
echo "   API: GET /api/prompt"
curl -s "$BASE_URL/api/prompt" | python -m json.tool | head -20
echo "   ..."

echo ""
echo "3️⃣  测试 Prompt 模板文件"
echo "   当前模板文件:"
ls -lh prompts/webhook_analysis*.txt 2>/dev/null || echo "   未找到模板文件"

echo ""
echo "4️⃣  演示热重载功能"
echo "   场景: 修改 prompt 文件后，无需重启服务即可生效"
echo ""
echo "   步骤:"
echo "   a) 备份当前 prompt"
if [ -f prompts/webhook_analysis.txt ]; then
    cp prompts/webhook_analysis.txt prompts/webhook_analysis.txt.backup
    echo "      ✅ 已备份到 prompts/webhook_analysis.txt.backup"
fi

echo ""
echo "   b) 切换到简化版 prompt"
if [ -f prompts/webhook_analysis_simple.txt ]; then
    cp prompts/webhook_analysis_simple.txt prompts/webhook_analysis.txt
    echo "      ✅ 已切换到简化版"
else
    echo "      ⚠️  简化版文件不存在，跳过"
fi

echo ""
echo "   c) 调用重载 API"
echo "      API: POST /api/prompt/reload"
RELOAD_RESULT=$(curl -s -X POST "$BASE_URL/api/prompt/reload")
echo "$RELOAD_RESULT" | python -m json.tool

echo ""
echo "   d) 验证是否生效"
NEW_LENGTH=$(echo "$RELOAD_RESULT" | python -c "import sys, json; print(json.load(sys.stdin).get('template_length', 0))")
echo "      新模板长度: $NEW_LENGTH 字符"

echo ""
echo "   e) 恢复原始 prompt"
if [ -f prompts/webhook_analysis.txt.backup ]; then
    mv prompts/webhook_analysis.txt.backup prompts/webhook_analysis.txt
    echo "      ✅ 已恢复原始配置"
    curl -s -X POST "$BASE_URL/api/prompt/reload" > /dev/null
    echo "      ✅ 已重新加载"
fi

echo ""
echo "5️⃣  可用的 API 端点"
echo ""
echo "   GET  /api/prompt         - 查看当前 prompt"
echo "   POST /api/prompt/reload  - 重新加载 prompt"
echo "   POST /api/config         - 更新系统配置"
echo ""

echo "=========================================="
echo "演示完成！"
echo "=========================================="
echo ""
echo "💡 提示:"
echo ""
echo "1. 修改 Prompt 模板:"
echo "   vim prompts/webhook_analysis.txt"
echo ""
echo "2. 热重载:"
echo "   curl -X POST $BASE_URL/api/prompt/reload"
echo ""
echo "3. 查看完整文档:"
echo "   cat PROMPT_CONFIG.md"
echo "   cat AI_PROMPT_USAGE.md"
echo ""
echo "4. 运行测试:"
echo "   python test_prompt_loading.py"
echo ""
