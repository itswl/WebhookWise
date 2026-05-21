#!/bin/bash
# 容器进程入口：只负责运行时初始化和 RUN_MODE 分发。

set -e

# 动态加载 jemalloc（适配 x86_64/aarch64）
JEMALLOC_PATH=$(find /usr/lib -name "libjemalloc.so.2" -print -quit 2>/dev/null)
if [ -n "$JEMALLOC_PATH" ]; then
    export LD_PRELOAD="$JEMALLOC_PATH"
fi

export API_WORKERS="${API_WORKERS:-4}"

case "${RUN_MODE:-api}" in
    migrate)
        echo "Starting in migration job mode..."
        exec python3 -m scripts.run_migrations
        ;;
    worker)
        echo "Starting in TaskIQ Worker mode..."
        exec taskiq worker --log-level "${THIRD_PARTY_LOG_LEVEL:-WARNING}" services.operations.taskiq_wiring:broker
        ;;
    scheduler)
        echo "Starting in TaskIQ Scheduler mode..."
        exec taskiq scheduler --log-level "${THIRD_PARTY_LOG_LEVEL:-WARNING}" services.operations.taskiq_wiring:scheduler --update-interval 5 --loop-interval 1
        ;;
    all)
        echo "Starting in all-in-one supervisor mode..."
        export PYTHONWARNINGS="${PYTHONWARNINGS:-ignore:pkg_resources.*:UserWarning}"
        exec supervisord -c /app/supervisord.conf
        ;;
    api|"")
        echo "Starting in API mode..."
        exec "$@"
        ;;
    *)
        echo "Unknown RUN_MODE=${RUN_MODE}" >&2
        exit 1
        ;;
esac
