from __future__ import annotations

from core.config import Config


class _SubConfigView:
    def __init__(self, provider: ConfigProvider, sub_config) -> None:
        self._provider = provider
        self._sub_config = sub_config

    def __getattr__(self, name: str):
        if self._provider.has(name):
            return self._provider.get(name)
        return getattr(self._sub_config, name)


class ConfigProvider:
    def __init__(self) -> None:
        self._values: dict[str, object] = {}

    def has(self, key: str) -> bool:
        return key in self._values

    def get(self, key: str, default=None):
        return self._values.get(key, default)

    def set(self, key: str, value) -> None:
        if value is None:
            self._values.pop(key, None)
        else:
            self._values[key] = value

    def snapshot(self) -> dict[str, object]:
        return dict(self._values)

    @property
    def ai(self) -> _SubConfigView:
        return _SubConfigView(self, Config.ai)

    @property
    def retry(self) -> _SubConfigView:
        return _SubConfigView(self, Config.retry)

    @property
    def server(self) -> _SubConfigView:
        return _SubConfigView(self, Config.server)


policies = ConfigProvider()
