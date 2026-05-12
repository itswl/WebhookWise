"""Notification channel contracts."""

from __future__ import annotations

from typing import Any, Protocol


class AsyncJsonPoster(Protocol):
    async def post(self, url: str, *, json: object, timeout: float | int | None = None) -> object: ...


class NotificationChannel(Protocol):
    @property
    def name(self) -> str: ...

    def supports(self, target_url: str) -> bool: ...

    async def send_card(self, target_url: str, card_payload: object) -> bool: ...

    async def send_deep_analysis(
        self,
        target_url: str,
        analysis_record: dict[str, Any],
        *,
        source: str = "",
        webhook_event_id: int = 0,
    ) -> bool: ...
