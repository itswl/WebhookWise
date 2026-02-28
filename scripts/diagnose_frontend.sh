#!/bin/bash
# 前端数据展示诊断脚本

echo "======================================"
echo "前端数据展示诊断"
echo "======================================"

BASE_URL="https://dejavu.prod.common-infra.hony.love"

echo ""
echo "1️⃣ 检查 API 是否返回数据..."
API_RESPONSE=$(curl -s "$BASE_URL/api/webhooks?page=1&page_size=1")
TOTAL=$(echo "$API_RESPONSE" | jq -r '.pagination.total')
SUCCESS=$(echo "$API_RESPONSE" | jq -r '.success')

if [ "$SUCCESS" = "true" ]; then
    echo "✅ API 正常，总记录数: $TOTAL"
else
    echo "❌ API 返回异常"
    echo "$API_RESPONSE" | jq .
    exit 1
fi

echo ""
echo "2️⃣ 检查最新5条数据..."
curl -s "$BASE_URL/api/webhooks?page=1&page_size=5" | jq '.data[] | {id, timestamp, source, importance, is_duplicate}'

echo ""
echo "3️⃣ 检查页面是否可访问..."
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" "$BASE_URL/")
if [ "$HTTP_CODE" = "200" ]; then
    echo "✅ 页面可访问 (HTTP $HTTP_CODE)"
else
    echo "❌ 页面访问失败 (HTTP $HTTP_CODE)"
fi

echo ""
echo "4️⃣ 检查静态资源..."
# 检查页面是否包含必要的 JavaScript
PAGE_CONTENT=$(curl -s "$BASE_URL/")
if echo "$PAGE_content" | grep -q "loadWebhooks"; then
    echo "✅ 页面包含 loadWebhooks 函数"
else
    echo "⚠️  页面可能缺少 JavaScript 函数"
fi

echo ""
echo "5️⃣ 模拟前端数据加载..."
echo "第一步：获取总数"
SUMMARY_RESPONSE=$(curl -s "$BASE_URL/api/webhooks?page=1&page_size=1&fields=summary")
TOTAL_CHECK=$(echo "$SUMMARY_RESPONSE" | jq -r '.pagination.total')
echo "   总记录数: $TOTAL_CHECK"

echo ""
echo "第二步：根据总数决定加载策略"
if [ "$TOTAL_CHECK" -le 1000 ]; then
    LOAD_SIZE=$TOTAL_CHECK
    STRATEGY="全部加载"
elif [ "$TOTAL_CHECK" -le 5000 ]; then
    LOAD_SIZE=3000
    STRATEGY="加载前 3000 条"
else
    LOAD_SIZE=5000
    STRATEGY="加载前 5000 条"
fi
echo "   策略: $STRATEGY (加载 $LOAD_SIZE 条)"

echo ""
echo "第三步：加载数据"
DATA_RESPONSE=$(curl -s "$BASE_URL/api/webhooks?page=1&page_size=$LOAD_SIZE&fields=summary")
DATA_COUNT=$(echo "$DATA_RESPONSE" | jq '.data | length')
echo "   实际加载: $DATA_COUNT 条"

if [ "$DATA_COUNT" -gt 0 ]; then
    echo ""
    echo "✅ 数据加载成功"
    echo ""
    echo "最新一条数据详情:"
    echo "$DATA_RESPONSE" | jq '.data[0]'
else
    echo ""
    echo "❌ 数据加载失败，未获取到数据"
fi

echo ""
echo "======================================"
echo "诊断完成"
echo "======================================"
echo ""
echo "💡 如果上述检查都正常但页面仍无数据，请："
echo "   1. 清除浏览器缓存 (Ctrl+Shift+Delete)"
echo "   2. 硬刷新页面 (Ctrl+Shift+R 或 Cmd+Shift+R)"
echo "   3. 打开浏览器开发者工具 (F12) 查看 Console 和 Network 标签"
echo "   4. 检查是否有 JavaScript 错误"
