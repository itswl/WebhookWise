"""已弃用：轮询器已迁移至 poller_scheduler.py

保留此文件仅为向后兼容 import。
"""

from services.poller_scheduler import start_scheduler as start_background_pollers  # noqa: F401
from services.poller_scheduler import stop_scheduler as stop_background_pollers  # noqa: F401
