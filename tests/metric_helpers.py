from __future__ import annotations

from typing import Any

MetricCall = tuple[str, tuple[object, ...], dict[str, object], str, object]
MetricValueCall = tuple[str, tuple[object, ...], dict[str, object], object]
MetricActionCall = tuple[str, tuple[object, ...], str, object]


class StubBoundMetric:
    def __init__(
        self,
        sink: list[Any],
        name: str,
        args: tuple[object, ...],
        kwargs: dict[str, object],
        *,
        record_action: bool,
        record_kwargs: bool,
    ) -> None:
        self._sink = sink
        self._name = name
        self._args = args
        self._kwargs = kwargs
        self._record_action = record_action
        self._record_kwargs = record_kwargs

    def _append(self, action: str, value: object) -> None:
        if self._record_action and self._record_kwargs:
            self._sink.append((self._name, self._args, self._kwargs, action, value))
            return
        if self._record_action:
            self._sink.append((self._name, self._args, action, value))
            return
        self._sink.append((self._name, self._args, self._kwargs, value))

    def inc(self, amount: object = 1) -> None:
        self._append("inc", amount)

    def dec(self, amount: object = 1) -> None:
        self._append("dec", amount)

    def set(self, value: object) -> None:
        self._append("set", value)

    def observe(self, value: object) -> None:
        self._append("observe", value)


class StubMetric:
    def __init__(
        self,
        sink: list[Any],
        name: str,
        *,
        record_action: bool = True,
        record_kwargs: bool = True,
    ) -> None:
        self._sink = sink
        self._name = name
        self._record_action = record_action
        self._record_kwargs = record_kwargs

    def labels(self, *args: object, **kwargs: object) -> StubBoundMetric:
        return StubBoundMetric(
            self._sink,
            self._name,
            args,
            kwargs,
            record_action=self._record_action,
            record_kwargs=self._record_kwargs,
        )

    def inc(self, amount: object = 1) -> None:
        self.labels().inc(amount)

    def dec(self, amount: object = 1) -> None:
        self.labels().dec(amount)

    def set(self, value: object) -> None:
        self.labels().set(value)

    def observe(self, value: object) -> None:
        self.labels().observe(value)
