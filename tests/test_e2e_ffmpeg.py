"""End-to-end test with the real ffmpeg binary.

Skipped when no ffmpeg is available. Verifies the actual complaint scenario:
a slow first run is overtaken by a backup, the final cut has all three
segments in order and the expected duration/specification.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import media  # noqa: E402
from app.config import Settings  # noqa: E402
from app.scheduler import BatchManager  # noqa: E402


def _ffmpeg() -> str | None:
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


FFMPEG = _ffmpeg()


@pytest.mark.skipif(FFMPEG is None, reason="real ffmpeg not available")
def test_real_ffmpeg_slow_a_produces_ordered_cut(tmp_path: Path):
    settings = Settings()
    settings.data_dir = tmp_path / "data"
    settings.ffmpeg_binary = FFMPEG
    settings.slots = 2
    settings.slow_after_seconds = 1.0
    settings.monitor_interval = 0.05
    settings.cancel_grace_seconds = 2.0
    settings.clip_seconds = 2
    settings.slow_readrate = 0.25  # ~8s for a 2s clip
    for sub in ("inputs", "work", "outputs"):
        (settings.data_dir / sub).mkdir(parents=True, exist_ok=True)

    media.ensure_fixtures(settings)
    manager = BatchManager(settings)
    try:
        batch = manager.create_batch("A")
        deadline = time.monotonic() + 40
        final = None
        while time.monotonic() < deadline:
            data = manager.get_batch(batch["id"])
            if data["status"] in ("ready", "failed"):
                final = data
                break
            time.sleep(0.05)
        assert final is not None, "batch did not settle"
        assert final["status"] == "ready", final["error"]

        attempts = final["attempts"]
        first = next(a for a in attempts if a["segment"] == "A" and a["kind"] == "first")
        backup = next(a for a in attempts if a["segment"] == "A" and a["kind"] == "backup")
        assert backup["status"] == "succeeded"
        assert final["segments"][0]["selected_attempt_id"] == backup["id"]
        assert first["status"] in ("cancelling", "cancelled", "superseded")

        path = manager.get_batch_file(batch["id"])
        assert path is not None and path.is_file()
        duration = media.probe_duration(FFMPEG, path)
        # Three 2-second segments.
        assert duration is not None and 5.5 <= duration <= 7.0, duration

        probe = subprocess.run(
            [FFMPEG, "-hide_banner", "-i", str(path), "-f", "null", "-"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
            check=False,
        )
        text = probe.stderr.decode("utf-8", "replace")
        assert "640x360" in text
        assert "30 fps" in text
        # A mid-stream reconfiguration would signal mismatched segment specs.
        assert "Reconfiguring filter graph" not in text

        # The cancelled slow process eventually exits and releases its slot.
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            data = manager.get_batch(batch["id"])
            if all(a["status"] not in ("running", "cancelling") for a in data["attempts"]):
                break
            time.sleep(0.1)
        assert all(a["status"] not in ("running", "cancelling") for a in data["attempts"])
        assert data["slots_used"] == 0
    finally:
        manager.shutdown()


def _make_settings(tmp_path: Path) -> Settings:
    settings = Settings()
    settings.data_dir = tmp_path / "data"
    settings.ffmpeg_binary = FFMPEG
    settings.slots = 2
    settings.slow_after_seconds = 0.5
    settings.monitor_interval = 0.05
    settings.cancel_grace_seconds = 2.0
    settings.clip_seconds = 2
    settings.slow_readrate = 0.25  # ~8s for a 2s clip
    for sub in ("inputs", "work", "outputs"):
        (settings.data_dir / sub).mkdir(parents=True, exist_ok=True)
    media.ensure_fixtures(settings)
    return settings


def _settle(manager, batch_id: str, timeout: float = 40) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        data = manager.get_batch(batch_id)
        if data["status"] in ("ready", "failed", "exceeded"):
            return data
        time.sleep(0.05)
    raise AssertionError("batch did not settle")


@pytest.mark.skipif(FFMPEG is None, reason="real ffmpeg not available")
def test_real_ffmpeg_budget_exceeded_stops_and_withholds(tmp_path: Path):
    settings = _make_settings(tmp_path)
    manager = BatchManager(settings)
    try:
        batch = manager.create_batch("A", slot_budget=1)
        final = _settle(manager, batch["id"])
        assert final["status"] == "exceeded", final["error"]
        assert final["budget_exceeded"] is True
        assert final["download_ready"] is False
        assert manager.get_batch_file(batch["id"]) is None
        assert final["slot_seconds_used"] >= 1
        # Wait for every cancelled process so the global pool is clean.
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            data = manager.get_batch(batch["id"])
            if not any(a["slot_held"] for a in data["attempts"]):
                break
            time.sleep(0.05)
        assert manager.slots_used == 0
        assert data["status"] == "exceeded"
    finally:
        manager.shutdown()


@pytest.mark.skipif(FFMPEG is None, reason="real ffmpeg not available")
def test_real_ffmpeg_generous_budget_is_metered_and_downloadable(tmp_path: Path):
    settings = _make_settings(tmp_path)
    manager = BatchManager(settings)
    try:
        batch = manager.create_batch("A", slot_budget=60)
        final = _settle(manager, batch["id"])
        assert final["status"] == "ready", final["error"]
        assert final["download_ready"] is True
        assert final["slot_seconds_used"] > 0
        assert final["slot_seconds_used"] < 60
        path = manager.get_batch_file(batch["id"])
        assert path is not None and path.is_file()
        duration = media.probe_duration(FFMPEG, path)
        assert duration is not None and 5.5 <= duration <= 7.0, duration
        assert not any(a["slot_held"] for a in final["attempts"])
    finally:
        manager.shutdown()
