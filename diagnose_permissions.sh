#!/bin/bash
# 配置文件权限诊断脚本

set -e

echo "=========================================="
echo "配置文件权限诊断"
echo "=========================================="

ENV_FILE=".env"

echo ""
echo "1️⃣  环境信息"
echo "   当前用户: $(whoami)"
echo "   用户 ID: $(id -u)"
echo "   组 ID: $(id -g)"
echo "   工作目录: $(pwd)"

if [ -n "$DOCKER_CONTAINER" ]; then
    echo "   运行环境: Docker 容器 ✓"
else
    echo "   运行环境: 本地系统"
fi

echo ""
echo "2️⃣  .env 文件状态"
if [ -f "$ENV_FILE" ]; then
    echo "   ✅ 文件存在"
    echo "   文件大小: $(wc -c < "$ENV_FILE") bytes"
    echo "   权限: $(ls -l "$ENV_FILE" | awk '{print $1}')"
    echo "   所有者: $(ls -l "$ENV_FILE" | awk '{print $3":"$4}')"

    # 检查扩展属性（仅 macOS）
    if command -v xattr &> /dev/null; then
        XATTRS=$(xattr -l "$ENV_FILE" 2>/dev/null || echo "")
        if [ -n "$XATTRS" ]; then
            echo "   ⚠️  扩展属性:"
            echo "$XATTRS" | sed 's/^/      /'
        else
            echo "   ✅ 无扩展属性"
        fi
    fi
else
    echo "   ❌ 文件不存在"
    exit 1
fi

echo ""
echo "3️⃣  权限测试"

# 测试读权限
if [ -r "$ENV_FILE" ]; then
    echo "   ✅ 读权限正常"
else
    echo "   ❌ 无读权限"
fi

# 测试写权限
if [ -w "$ENV_FILE" ]; then
    echo "   ✅ 写权限正常"
else
    echo "   ❌ 无写权限"
    echo ""
    echo "   建议修复命令:"
    echo "   chmod 644 $ENV_FILE"
    echo "   chown $(whoami):$(id -gn) $ENV_FILE"
fi

# 测试实际写入
echo ""
echo "4️⃣  写入测试"
TEST_FILE="${ENV_FILE}.test"
if echo "TEST=true" > "$TEST_FILE" 2>/dev/null; then
    echo "   ✅ 文件写入成功"
    rm -f "$TEST_FILE"

    # 测试追加
    if echo "# Test" >> "$ENV_FILE" 2>/dev/null; then
        echo "   ✅ 文件追加成功"
        # 恢复（删除测试行）
        if command -v sed &> /dev/null; then
            # macOS 和 Linux 兼容
            sed -i.bak '/^# Test$/d' "$ENV_FILE" 2>/dev/null || true
            rm -f "${ENV_FILE}.bak"
        fi
    else
        echo "   ❌ 文件追加失败"
    fi
else
    echo "   ❌ 文件写入失败"
fi

echo ""
echo "5️⃣  Python 写入测试"
python3 << 'EOF'
from pathlib import Path
import sys

env_file = Path('.env')

try:
    # 读取测试
    content = env_file.read_text()
    print("   ✅ Python 读取成功")

    # 写入测试（临时文件）
    temp = env_file.with_suffix('.env.pytest')
    temp.write_text("TEST=true\n")
    print("   ✅ Python 写入成功")

    # 替换测试
    backup = env_file.with_suffix('.env.bak')
    env_file.replace(backup)
    backup.replace(env_file)
    print("   ✅ Python 文件替换成功")

    # 清理
    if temp.exists():
        temp.unlink()

except PermissionError as e:
    print(f"   ❌ Python 权限错误: {e}", file=sys.stderr)
    sys.exit(1)
except Exception as e:
    print(f"   ❌ Python 其他错误: {e}", file=sys.stderr)
    sys.exit(1)
EOF

if [ $? -eq 0 ]; then
    echo ""
    echo "=========================================="
    echo "✅ 诊断完成 - 权限正常"
    echo "=========================================="
    echo ""
    echo "配置文件可以正常读写。"
    echo "如果仍然出现权限错误，可能是："
    echo "1. Docker 容器内外权限不一致"
    echo "2. SELinux 或 AppArmor 限制"
    echo "3. 文件系统挂载为只读"
    echo ""
    echo "建议："
    echo "- 使用环境变量配置（推荐）"
    echo "- 或查看 CONFIG_SAVE_ISSUE.md 获取详细解决方案"
else
    echo ""
    echo "=========================================="
    echo "❌ 诊断完成 - 发现权限问题"
    echo "=========================================="
    echo ""
    echo "修复建议："
    echo ""
    echo "1. 修复文件权限："
    echo "   chmod 644 $ENV_FILE"
    echo "   chown $(whoami):$(id -gn) $ENV_FILE"
    echo ""
    echo "2. 移除扩展属性（macOS）："
    if command -v xattr &> /dev/null; then
        echo "   xattr -c $ENV_FILE"
    fi
    echo ""
    echo "3. 使用环境变量（推荐）："
    echo "   在 docker-compose.yml 中配置 environment"
    echo ""
    echo "详细文档: CONFIG_SAVE_ISSUE.md"
    exit 1
fi
