"""Liveness-only healthcheck mode: local heartbeat file, no DB/Redis access."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from scripts.healthcheck import main


def test_live_mode_passes_on_fresh_heartbeat_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    heartbeat = tmp_path / "heartbeat"
    heartbeat.write_text(str(time.time()), encoding="utf-8")
    monkeypatch.setenv("WEBHOOK_LOCAL_HEARTBEAT_FILE", str(heartbeat))

    assert main(["--live"]) == 0


def test_live_mode_fails_on_stale_heartbeat_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    heartbeat = tmp_path / "heartbeat"
    heartbeat.write_text("stale", encoding="utf-8")
    stale_mtime = time.time() - 3600
    os.utime(heartbeat, (stale_mtime, stale_mtime))
    monkeypatch.setenv("WEBHOOK_LOCAL_HEARTBEAT_FILE", str(heartbeat))

    assert main(["--live"]) == 1


def test_live_mode_fails_on_missing_heartbeat_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEBHOOK_LOCAL_HEARTBEAT_FILE", str(tmp_path / "missing"))

    assert main(["--live"]) == 1


def test_live_mode_ignores_blank_env_and_uses_default_path(monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts import healthcheck

    monkeypatch.setenv("WEBHOOK_LOCAL_HEARTBEAT_FILE", "   ")

    observed: list[str] = []

    def missing_stat(path: str, *args: object, **kwargs: object) -> os.stat_result:
        observed.append(path)
        raise FileNotFoundError(path)

    monkeypatch.setattr(healthcheck.os, "stat", missing_stat)

    assert healthcheck._local_heartbeat_is_fresh() is False
    assert observed == [healthcheck.LOCAL_HEARTBEAT_FILE_DEFAULT]
