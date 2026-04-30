"""Gunicorn 配置 — Prometheus 多进程指标清理钩子。"""


def child_exit(server, worker):
    """Worker 退出时清理其 Prometheus 指标文件。"""
    try:
        from prometheus_client import multiprocess

        multiprocess.mark_process_dead(worker.pid)
    except Exception:
        pass
