"""Runtime configuration for the preview-batch demo."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

# Per-batch FFmpeg slot-second budget accepted by the creation API.
MIN_SLOT_BUDGET_SECONDS = 1
MAX_SLOT_BUDGET_SECONDS = 120

_SLOT_BUDGET_ERROR = (
    f"槽秒预算必须是 {MIN_SLOT_BUDGET_SECONDS}~{MAX_SLOT_BUDGET_SECONDS} "
    "的整数，或留空"
)


def normalize_slot_budget(raw: Any) -> int | None:
    """Normalize an optional slot-second budget.

    ``None`` and blank strings mean "no budget" (original flow). Integers and
    integer-valued strings/floats are accepted; booleans, fractions, and values
    outside 1..120 are rejected.
    """

    if raw is None:
        return None
    if isinstance(raw, bool):
        raise ValueError(_SLOT_BUDGET_ERROR)
    if isinstance(raw, str):
        text = raw.strip()
        if text == "":
            return None
        try:
            value = int(text)
        except ValueError as exc:
            raise ValueError(_SLOT_BUDGET_ERROR) from exc
    elif isinstance(raw, int):
        value = raw
    elif isinstance(raw, float) and raw.is_integer():
        value = int(raw)
    else:
        raise ValueError(_SLOT_BUDGET_ERROR)
    if not MIN_SLOT_BUDGET_SECONDS <= value <= MAX_SLOT_BUDGET_SECONDS:
        raise ValueError(_SLOT_BUDGET_ERROR)
    return value


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
