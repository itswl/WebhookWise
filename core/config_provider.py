from __future__ import annotations

from datetime import datetime

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
        self._meta: dict[str, dict[str, object]] = {}

    def has(self, key: str) -> bool:
        return key in self._values

    def get(self, key: str, default=None):
        return self._values.get(key, default)

    def set(
        self,
        key: str,
        value,
        *,
        source: str | None = None,
        updated_at: datetime | None = None,
        updated_by: str | None = None,
    ) -> None:
        if value is None:
            self._values.pop(key, None)
            self._meta.pop(key, None)
        else:
            self._values[key] = value
            meta = self._meta.get(key, {})
            if source is not None:
                meta["source"] = source
            if updated_at is not None:
                meta["updated_at"] = updated_at
            if updated_by is not None:
                meta["updated_by"] = updated_by
            if meta:
                self._meta[key] = meta

    def meta(self, key: str) -> dict[str, object]:
        return dict(self._meta.get(key, {}))

    def snapshot(self) -> dict[str, object]:
        return dict(self._values)

    def snapshot_meta(self) -> dict[str, dict[str, object]]:
        return {k: dict(v) for k, v in self._meta.items()}

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
