import logging
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

WebhookData = dict[str, Any]

class AdapterRegistry:
    def __init__(self):
        self._detectors: list[tuple[str, Callable[[WebhookData], bool]]] = []
        self._normalizers: dict[str, Callable[[WebhookData], WebhookData]] = {}

    def register(self, source_name: str):
        """装饰器：注册一个生态适配器"""
        def wrapper(func: Callable[[WebhookData], WebhookData]):
            self._normalizers[source_name] = func
            logger.debug(f"[Adapter Registry] Registered normalizer for: {source_name}")
            return func
        return wrapper

    def register_detector(self, source_name: str):
        """装饰器：注册一个生态适配器探测器"""
        def wrapper(func: Callable[[WebhookData], bool]):
            self._detectors.append((source_name, func))
            logger.debug(f"[Adapter Registry] Registered detector for: {source_name}")
            return func
        return wrapper

    def find_adapter_by_payload(self, data: WebhookData) -> str | None:
        """根据 Payload 特征匹配适配器"""
        for source_name, detector in self._detectors:
            try:
                if detector(data):
                    return source_name
            except Exception:
                pass
        return None

    def get_normalizer(self, source_name: str) -> Callable[[WebhookData], WebhookData] | None:
        """获取特定适配器的归一化函数"""
        return self._normalizers.get(source_name)

registry = AdapterRegistry()
