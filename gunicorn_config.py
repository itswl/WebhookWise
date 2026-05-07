"""Gunicorn 配置 — Prometheus 多进程指标清理钩子。"""

from collections.abc import Callable
from typing import Any


def child_exit(server: Any, worker: Any) -> None:
    """Worker 退出时清理其 Prometheus 指标文件。"""
    try:
        from prometheus_client import multiprocess

        mark_dead: Callable[[int], None] = multiprocess.mark_process_dead
        mark_dead(int(worker.pid))
    except Exception:  # nosec B110
        pass
