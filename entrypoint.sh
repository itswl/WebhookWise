#!/bin/bash
# 容器启动脚本 - 自动执行数据库初始化和迁移

set -e  # 遇到错误立即退出

# 动态加载 jemalloc（兼容 x86_64/aarch64）
JEMALLOC_PATH=$(find /usr/lib -name "libjemalloc.so.2" -print -quit 2>/dev/null)
if [ -n "$JEMALLOC_PATH" ]; then
    export LD_PRELOAD="$JEMALLOC_PATH"
fi

# 清理 Prometheus 多进程残留文件
if [ -n "$PROMETHEUS_MULTIPROC_DIR" ] && [ -d "$PROMETHEUS_MULTIPROC_DIR" ]; then
    rm -rf "${PROMETHEUS_MULTIPROC_DIR:?}"/*
fi

# Worker 模式：启动 TaskIQ Worker 和 Scheduler
if [ "$RUN_MODE" = "worker" ]; then
    echo "Starting in TaskIQ Worker mode..."
    # 等待数据库就绪
    max_retries=30
    retry_count=0
    while [ $retry_count -lt $max_retries ]; do
        if python3 -c "import asyncio; from db.session import init_engine, test_db_connection
async def _check():
    await init_engine()
    return await test_db_connection()
exit(0 if asyncio.run(_check()) else 1)" 2>/dev/null; then
            echo "✅ Worker: 数据库连接成功"
            break
        else
            retry_count=$((retry_count + 1))
            echo "⏳ Worker: 等待数据库... ($retry_count/$max_retries)"
            sleep 2
        fi
    done
    if [ $retry_count -eq $max_retries ]; then
        echo "❌ Worker: 数据库连接超时，启动失败"
        exit 1
    fi
    # 启动 TaskIQ Worker (所有定时轮询任务已迁移至 receiver 进程的 asyncio 循环)
    exec taskiq worker core.taskiq_broker:broker services.tasks
fi

# 以下是 API / all 模式的启动流程

echo "======================================"
echo "Webhook 服务启动中..."
echo "======================================"

# [1/3] 等待数据库就绪
echo "[1/3] 等待数据库就绪..."
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

# [2/3] Alembic 迁移（所有 DDL 变更的唯一入口）
echo "[2/3] Alembic 迁移..."
if ! (cd /app && alembic upgrade head) 2>&1; then
    echo "⚠️ Alembic 迁移失败，请检查日志。数据库 Schema 可能不完整"
    if [ "${ALLOW_START_WITHOUT_MIGRATION:-false}" != "true" ]; then
        exit 1
    fi
fi

# 极端兜底：如果 DB 实际对象已存在但 alembic_version 未推进，会导致每次启动重复跑同一条幂等迁移
python3 - << "PY"
import os

import psycopg2

url = os.environ.get("DATABASE_URL", "")
if not url:
    raise SystemExit(0)

conn = psycopg2.connect(url)
conn.autocommit = True
cur = conn.cursor()
cur.execute("select version_num from public.alembic_version")
current = cur.fetchone()[0]

if current == "9c0b7c3e2a11":
    cur.execute("select to_regclass('public.processing_locks')")
    has_processing_locks = bool(cur.fetchone()[0])
    cur.execute(
        "select 1 from pg_indexes where schemaname='public' and indexname='idx_unique_alert_hash_original' limit 1"
    )
    has_unique_idx = cur.fetchone() is not None

    if has_processing_locks and has_unique_idx:
        cur.execute("update public.alembic_version set version_num=%s", ("6a7b8c9d0e1f",))

cur.close()
conn.close()
PY

echo "✅ Alembic 迁移完成"

echo "======================================"
echo "数据库准备完成，启动应用服务..."
echo "======================================"

# [3/3] 启动应用
exec "$@"
