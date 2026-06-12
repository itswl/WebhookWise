#!/bin/bash
# 容器进程入口：只负责运行时初始化和 RUN_MODE 分发。

set -e

# 动态加载 jemalloc（适配 x86_64/aarch64）
JEMALLOC_PATH=$(find /usr/lib -name "libjemalloc.so.2" -print -quit 2>/dev/null)
if [ -n "$JEMALLOC_PATH" ]; then
    if command -v ldd >/dev/null 2>&1 && ldd --version 2>&1 | grep -qi musl; then
        echo "jemalloc preload skipped (musl libc detected)"
    elif command -v ldd >/dev/null 2>&1 && ! ldd "$JEMALLOC_PATH" >/dev/null 2>&1; then
        echo "jemalloc preload skipped (incompatible binary)"
    else
        export LD_PRELOAD="$JEMALLOC_PATH${LD_PRELOAD:+:$LD_PRELOAD}"
    fi
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
        exec taskiq scheduler \
            --log-level "${THIRD_PARTY_LOG_LEVEL:-WARNING}" \
            services.operations.taskiq_wiring:scheduler \
            --update-interval "${TASKIQ_SCHEDULER_UPDATE_INTERVAL_SECONDS:-30}" \
            --loop-interval "${TASKIQ_SCHEDULER_LOOP_INTERVAL_SECONDS:-1}"
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
