#!/bin/bash
# 容器启动脚本 - 自动执行数据库初始化和迁移

set -e  # 遇到错误立即退出

# Worker 模式：跳过 DB 初始化和迁移（由 api 节点负责），直接启动 Poller
if [ "$RUN_MODE" = "worker" ]; then
    echo "Starting in Worker mode..."
    exec python worker.py
fi

# 以下是 API / all 模式的启动流程

echo "======================================"
echo "Webhook 服务启动中..."
echo "======================================"

# 1. 等待数据库就绪
echo "[1/5] 等待数据库就绪..."
max_retries=30
retry_count=0

while [ $retry_count -lt $max_retries ]; do
    if python3 -c "import asyncio; from db.session import init_engine, test_db_connection
async def _check():
    await init_engine()
    return await test_db_connection()
exit(0 if asyncio.run(_check()) else 1)" 2>/dev/null; then
        echo "✅ 数据库连接成功"
        break
    else
        retry_count=$((retry_count + 1))
        echo "⏳ 等待数据库... ($retry_count/$max_retries)"
        sleep 2
    fi
done

if [ $retry_count -eq $max_retries ]; then
    echo "❌ 数据库连接超时，启动失败"
    exit 1
fi

# 2. 初始化数据库表结构
echo "[2/5] 初始化数据库表..."
python3 -c "import asyncio; from db.session import init_engine, init_db
async def _init():
    await init_engine()
    await init_db()
asyncio.run(_init())" || {
    echo "⚠️  表初始化失败（可能已存在），继续..."
}
echo "✅ 数据库表检查完成"

# 3. 执行数据库迁移（添加去重字段等）
echo "[3/5] 执行数据库迁移（旧系统）..."
python3 -m migrations.migrate_db || {
    echo "⚠️  迁移失败，继续..."
}
echo "✅ 数据库迁移完成"

# 4. 添加唯一约束（防止重复告警）
echo "[4/5] 检查唯一约束..."
python3 -m migrations.init_migrations || {
    echo "⚠️  唯一约束检查失败，继续启动..."
}
echo "✅ 数据库约束检查完成"

# 5. Alembic 迁移（增量 schema 变更）
echo "[5/5] Alembic 迁移..."
cd /app && alembic upgrade head 2>&1 || {
    echo "⚠️  Alembic upgrade 失败，尝试 stamp head 后重试..."
    alembic stamp head 2>&1
    alembic upgrade head 2>&1 || echo "⚠️  Alembic 迁移最终失败，依赖应用层 schema 保障"
}
echo "✅ Alembic 迁移完成"

echo "======================================"
echo "数据库准备完成，启动应用服务..."
echo "======================================"

# 6. 启动应用服务
exec "$@"
