import importlib
import logging
import pkgutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

WebhookData = dict[str, Any]


class AdapterRegistry:
    def __init__(self) -> None:
        self._detectors: list[tuple[str, Callable[[WebhookData], bool]]] = []
        self._normalizers: dict[str, Callable[[WebhookData], WebhookData]] = {}
        self._aliases: dict[str, str] = {}
        self._discovered: bool = False

    def register(self, source_name: str, *, aliases: set[str] | None = None):
        """装饰器：注册一个生态适配器，可选提供别名集合。"""

        def wrapper(func: Callable[[WebhookData], WebhookData]):
            self._normalizers[source_name] = func
            # 注册别名映射
            normalized_key = source_name.lower().replace(" ", "")
            self._aliases[normalized_key] = source_name
            if aliases:
                for alias in aliases:
                    self._aliases[alias.lower().replace(" ", "")] = source_name
            logger.debug(f"[Adapter Registry] Registered normalizer for: {source_name}")
            return func

        return wrapper

    def register_detector(self, source_name: str):
        """装饰器：注册一个生态适配器探测器。"""

        def wrapper(func: Callable[[WebhookData], bool]):
            self._detectors.append((source_name, func))
            logger.debug(f"[Adapter Registry] Registered detector for: {source_name}")
            return func

        return wrapper

    def find_adapter_by_payload(self, data: WebhookData) -> str | None:
        """根据 Payload 特征匹配适配器。"""
        for source_name, detector in self._detectors:
            try:
                if detector(data):
                    return source_name
            except Exception:  # nosec B110 # noqa: PERF203
                pass
        return None

    def find_adapter_by_source(self, source: str) -> str | None:
        """通过来源名称或别名查找适配器名。

        将 source 小写化并去除空格后在别名映射中查找。
        """
        key = source.lower().replace(" ", "")
        return self._aliases.get(key)

    def get_normalizer(self, source_name: str) -> Callable[[WebhookData], WebhookData] | None:
        """获取特定适配器的归一化函数。"""
        return self._normalizers.get(source_name)

    def normalize(self, adapter_name: str, data: WebhookData) -> WebhookData:
        """便捷方法：获取 normalizer 并对数据执行归一化。

        若适配器不存在则原样返回数据。
        """
        normalizer = self.get_normalizer(adapter_name)
        if normalizer is None:
            logger.warning(f"[Adapter Registry] No normalizer found for: {adapter_name}")
            return data
        return normalizer(data)

    def auto_discover(self) -> None:
        """自动导入 adapters/plugins/ 下所有 .py 模块，触发装饰器注册。

        幂等：多次调用只执行一次导入。
        """
        if self._discovered:
            return

        plugins_dir = Path(__file__).parent / "plugins"
        if not plugins_dir.is_dir():
            logger.warning("[Adapter Registry] plugins directory not found")
            self._discovered = True
            return

        package_name = "adapters.plugins"
        for module_info in pkgutil.iter_modules([str(plugins_dir)]):
            if module_info.name.startswith("_"):
                continue
            module_full = f"{package_name}.{module_info.name}"
            try:
                importlib.import_module(module_full)
                logger.debug(f"[Adapter Registry] Auto-discovered: {module_full}")
            except Exception as e:
                logger.error(f"[Adapter Registry] Failed to import {module_full}: {e}")

        self._discovered = True


registry = AdapterRegistry()
