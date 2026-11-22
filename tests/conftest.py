from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings  # noqa: E402
from app.scheduler import BatchManager  # noqa: E402

FAKE_FFMPEG = Path(__file__).with_name("fake_ffmpeg.py")
FAKE_CONFIG = Path(__file__).with_name("fake_config.json")


class Harness:
    def __init__(self, tmp_path: Path, *, slots: int = 2, slow_after: float = 0.35,
                 cancel_grace: float = 0.3, shutdown_grace: float = 2.0) -> None:
        self.tmp_path = tmp_path
        self.config_path = FAKE_CONFIG
        self.manager: BatchManager | None = None
        self.settings = Settings()
        self.settings.data_dir = tmp_path / "data"
        self.settings.ffmpeg_binary = str(FAKE_FFMPEG)
        self.settings.slots = slots
        self.settings.slow_after_seconds = slow_after
        self.settings.monitor_interval = 0.01
        self.settings.cancel_grace_seconds = cancel_grace
        self.settings.shutdown_grace_seconds = shutdown_grace
        self.settings.clip_seconds = 4
        self.write_fake_config({})

    def write_fake_config(self, config: dict) -> None:
        FAKE_CONFIG.write_text(json.dumps(config), encoding="utf-8")

    def start(self) -> BatchManager:
        for sub in ("inputs", "work", "outputs"):
            (self.settings.data_dir / sub).mkdir(parents=True, exist_ok=True)
        self.manager = BatchManager(self.settings)
        return self.manager

    def stop(self) -> None:
        if self.manager is not None:
            self.manager.shutdown()
            self.manager = None

    def wait_for(self, predicate, *, timeout: float = 8.0, interval: float = 0.01):
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            last = predicate()
            if last:
                return last
            time.sleep(interval)
        raise AssertionError(f"condition not met within {timeout}s; last={last!r}")

    def batch(self, batch_id: str) -> dict:
        assert self.manager is not None
        data = self.manager.get_batch(batch_id)
        assert data is not None
        return data


@pytest.fixture()
def harness(tmp_path: Path):
    h = Harness(tmp_path)
    try:
        yield h
    finally:
        h.stop()
        FAKE_CONFIG.unlink(missing_ok=True)


@pytest.fixture()
def started(harness: Harness):
    harness.start()
    return harness
