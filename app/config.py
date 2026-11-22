"""Runtime configuration for the preview-batch demo."""

from __future__ import annotations

import os
from pathlib import Path


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


BASE_DIR = Path(__file__).resolve().parent


def _resolve_ffmpeg() -> str:
    explicit = os.environ.get("FFMPEG_BINARY", "").strip()
    if explicit:
        return explicit
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"


class Settings:
    """Settings are read from environment variables once at import time."""

    def __init__(self) -> None:
        self.data_dir = Path(os.environ.get("DATA_DIR", "data"))
        self.ffmpeg_binary = _resolve_ffmpeg()
        self.slots = max(1, _env_int("TRANSCODE_SLOTS", 2))
        self.slow_after_seconds = max(0.0, _env_float("SLOW_AFTER_SECONDS", 2.0))
        # Read rate passed to ffmpeg's -readrate for the simulated slow first run.
        # 0.4 means roughly 2.5x the clip duration, which clears the 2s threshold.
        self.slow_readrate = min(1.0, max(0.05, _env_float("SLOW_READRATE", 0.4)))
        # How often a running attempt is checked against the slow threshold.
        self.monitor_interval = max(0.01, _env_float("MONITOR_INTERVAL", 0.1))
        # Grace period before escalating a cancel from SIGTERM to SIGKILL.
        self.cancel_grace_seconds = max(0.0, _env_float("CANCEL_GRACE_SECONDS", 5.0))
        # Default clip duration when fixtures must be generated.
        self.clip_seconds = max(1, _env_int("CLIP_SECONDS", 4))
        # Composition can start while cancelled ffmpeg processes are still
        # exiting; this is how long cleanup may take before we give up waiting.
        self.shutdown_grace_seconds = max(0.0, _env_float("SHUTDOWN_GRACE_SECONDS", 8.0))
        self.clip_names = ("A", "B", "C")

    @property
    def input_dir(self) -> Path:
        return self.data_dir / "inputs"

    @property
    def work_dir(self) -> Path:
        return self.data_dir / "work"

    @property
    def output_dir(self) -> Path:
        return self.data_dir / "outputs"


settings = Settings()
