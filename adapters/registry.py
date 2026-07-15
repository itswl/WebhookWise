from collections.abc import Callable
from typing import TypeVar

from contracts.webhook_payload import JsonObject, WebhookData, webhook_data_from_mapping
from core.logger import get_logger

logger = get_logger("adapters.registry")

_Normalizer = Callable[[JsonObject], WebhookData]
_Detector = Callable[[JsonObject], bool]
_FNorm = TypeVar("_FNorm", bound=_Normalizer)
_FDet = TypeVar("_FDet", bound=_Detector)


class AdapterRegistry:
    def __init__(self) -> None:
        self._detectors: list[tuple[int, int, str, Callable[[JsonObject], bool]]] = []
        self._detector_seq = 0
        self._normalizers: dict[str, Callable[[JsonObject], WebhookData]] = {}
        self._aliases: dict[str, str] = {}

    def register(self, source_name: str, *, aliases: set[str] | None = None) -> Callable[[_FNorm], _FNorm]:
        """Decorator: register an ecosystem adapter, optionally providing a set of aliases."""

        def wrapper(func: _FNorm) -> _FNorm:
            self._normalizers[source_name] = func
            # Register alias mappings
            normalized_key = source_name.lower().replace(" ", "")
            self._aliases[normalized_key] = source_name
            if aliases:
                for alias in aliases:
                    self._aliases[alias.lower().replace(" ", "")] = source_name
            logger.debug("[Adapter Registry] Registered normalizer for: %s", source_name)
            return func

        return wrapper

    def register_detector(self, source_name: str, *, priority: int = 0) -> Callable[[_FDet], _FDet]:
        """Decorator: register an ecosystem adapter detector.

        ``priority`` controls detection order: detectors run highest priority
        first, ties broken by registration order. Built-in code adapters
        register at the default priority (0) before any declarative specs, so a
        declarative adapter must set a positive ``priority`` to win a detect tie
        against a code adapter (and a negative one to defer to every other
        detector). See adapters/declarative.py.
        """

        def wrapper(func: _FDet) -> _FDet:
            self._detectors.append((priority, self._detector_seq, source_name, func))
            self._detector_seq += 1
            # Re-sort on each registration (startup-only, tiny N) so the hot
            # find_adapter_by_payload path iterates in priority order without
            # sorting per lookup: highest priority first, then insertion order.
            self._detectors.sort(key=lambda entry: (-entry[0], entry[1]))
            logger.debug("[Adapter Registry] Registered detector for: %s (priority=%d)", source_name, priority)
            return func

        return wrapper

    def find_adapter_by_payload(self, data: JsonObject) -> str | None:
        """Match an adapter based on payload characteristics."""
        for _priority, _seq, source_name, detector in self._detectors:
            try:
                if detector(data):
                    return source_name
            except (AttributeError, KeyError, RuntimeError, TypeError, ValueError):  # noqa: PERF203
                logger.exception("[Adapter Registry] Detector failed for %s", source_name)
        return None

    def find_adapter_by_source(self, source: str) -> str | None:
        """Look up an adapter name by source name or alias.

        Lowercases the source and strips spaces before looking it up in the alias map.
        """
        key = source.lower().replace(" ", "")
        return self._aliases.get(key)

    def get_normalizer(self, source_name: str) -> Callable[[JsonObject], WebhookData] | None:
        """Get the normalization function for a specific adapter."""
        return self._normalizers.get(source_name)

    def normalize(self, adapter_name: str, data: JsonObject) -> WebhookData:
        """Convenience method: get the normalizer and normalize the data.

        Returns the data unchanged if the adapter does not exist.
        """
        normalizer = self.get_normalizer(adapter_name)
        if normalizer is None:
            logger.warning("[Adapter Registry] No normalizer found for: %s", adapter_name)
            return webhook_data_from_mapping(data, strict=False)
        return normalizer(data)

    def status(self) -> dict[str, int]:
        return {
            "detectors": len(self._detectors),
            "normalizers": len(self._normalizers),
            "aliases": len(self._aliases),
        }


registry = AdapterRegistry()
