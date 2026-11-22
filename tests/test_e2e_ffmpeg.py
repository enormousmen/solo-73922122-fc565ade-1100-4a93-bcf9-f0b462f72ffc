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
