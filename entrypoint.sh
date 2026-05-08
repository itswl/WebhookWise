#!/bin/bash
# 容器进程入口：只负责运行时初始化和 RUN_MODE 分发。

set -e

# 动态加载 jemalloc（兼容 x86_64/aarch64）
JEMALLOC_PATH=$(find /usr/lib -name "libjemalloc.so.2" -print -quit 2>/dev/null)
if [ -n "$JEMALLOC_PATH" ]; then
    export LD_PRELOAD="$JEMALLOC_PATH"
fi

# 清理 Prometheus 多进程残留文件
if [ -n "$PROMETHEUS_MULTIPROC_DIR" ] && [ -d "$PROMETHEUS_MULTIPROC_DIR" ]; then
    rm -rf "${PROMETHEUS_MULTIPROC_DIR:?}"/*
fi

case "${RUN_MODE:-api}" in
    migrate)
        echo "Starting in migration job mode..."
        exec python3 -m scripts.run_migrations
        ;;
    worker)
        echo "Starting in TaskIQ Worker mode..."
        exec taskiq worker core.taskiq_broker:broker services.operations.tasks
        ;;
    scheduler)
        echo "Starting in TaskIQ Scheduler mode..."
        exec taskiq scheduler core.taskiq_broker:scheduler --update-interval 5 --loop-interval 1
        ;;
    api|all|"")
        echo "Starting in API mode..."
        exec "$@"
        ;;
    *)
        echo "Unknown RUN_MODE=${RUN_MODE}" >&2
        exit 1
        ;;
esac
