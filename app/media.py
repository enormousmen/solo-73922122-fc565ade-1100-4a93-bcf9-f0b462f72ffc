"""FFmpeg helpers: input fixtures, the shared transcode command, probing, concat."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Iterable

from .config import Settings

# Every attempt — normal or slow — must land on this exact specification so the
# final concat stream copy has no mid-stream reconfiguration.
VIDEO_WIDTH = 640
VIDEO_HEIGHT = 360
VIDEO_FPS = 30
VIDEO_CRF = "23"
VIDEO_PRESET = "veryfast"
VIDEO_GOP = 60

FILTER_CHAIN = (
    f"scale={VIDEO_WIDTH}:{VIDEO_HEIGHT}:force_original_aspect_ratio=decrease,"
    f"pad={VIDEO_WIDTH}:{VIDEO_HEIGHT}:(ow-iw)/2:(oh-ih)/2:color=black,"
    f"setsar=1,fps={VIDEO_FPS},format=yuv420p"
)

# Visual identity of each built-in clip. Silent, deterministic, and cheap.
CLIP_SOURCES = {
    "A": "testsrc2=size=640x360:rate=30",
    "B": "smptebars=size=640x360:rate=30",
    "C": "rgbtestsrc=size=640x360:rate=30",
}


class MediaError(RuntimeError):
    """Raised when ffmpeg cannot produce a usable artifact."""


def _run(binary: str, args: list[str], *, timeout: float | None = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            [binary, "-hide_banner", "-loglevel", "error", *args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:  # pragma: no cover - depends on host
        raise MediaError(f"ffmpeg binary not found: {binary}") from exc
    except subprocess.TimeoutExpired as exc:
        raise MediaError("ffmpeg timed out") from exc


def ensure_fixtures(settings: Settings) -> list[Path]:
    """Create the three built-in silent clips once per data directory.

    The files are tiny (seconds, 640x360, no audio) and are derived from lavfi
    sources, so no binary assets need to be stored in the repository.
    """

    settings.input_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for index, name in enumerate(settings.clip_names, start=1):
        target = settings.input_dir / f"clip_{index}_{name}.mp4"
        paths.append(target)
        if target.is_file() and target.stat().st_size > 0:
            continue
        tmp = target.with_suffix(".tmp.mp4")
        result = _run(
            settings.ffmpeg_binary,
            [
                "-y",
                "-f",
                "lavfi",
                "-i",
                CLIP_SOURCES[name],
                "-t",
                str(settings.clip_seconds),
                "-an",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                VIDEO_CRF,
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
                str(tmp),
            ],
            timeout=90,
        )
        if result.returncode != 0 or not tmp.is_file() or tmp.stat().st_size == 0:
            tmp.unlink(missing_ok=True)
            detail = result.stderr.decode("utf-8", "replace").strip()[-800:]
            raise MediaError(f"could not create fixture {target.name}: {detail}")
        os.replace(tmp, target)
    return paths


def transcode_command(
    settings: Settings,
    source: Path,
    target: Path,
    *,
    slow: bool,
) -> list[str]:
    """Build one attempt's command.

    ``slow=True`` throttles ffmpeg's input reading via ``-readrate``. The
    artifact is still produced with the identical encode settings, so a slow
    attempt is a genuine candidate, not a placeholder.
    """

    args = [
        settings.ffmpeg_binary,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
    ]
    if slow:
        args += ["-readrate", f"{settings.slow_readrate}"]
    args += [
        "-i",
        str(source),
        "-map",
        "0:v:0",
        "-an",
        "-sn",
        "-dn",
        "-vf",
        FILTER_CHAIN,
        "-map_metadata",
        "-1",
        "-c:v",
        "libx264",
        "-preset",
        VIDEO_PRESET,
        "-crf",
        VIDEO_CRF,
        "-profile:v",
        "high",
        "-level",
        "4.0",
        "-g",
        str(VIDEO_GOP),
        "-keyint_min",
        str(VIDEO_GOP),
        "-sc_threshold",
        "0",
        "-pix_fmt",
        "yuv420p",
        "-video_track_timescale",
        "90000",
        "-color_primaries",
        "bt709",
        "-color_trc",
        "bt709",
        "-colorspace",
        "bt709",
        "-movflags",
        "+faststart",
        "-f",
        "mp4",
        str(target),
    ]
    return args


def probe_duration(ffmpeg_binary: str, path: Path) -> float | None:
    """Read a container duration with ffmpeg itself (no ffprobe dependency)."""

    info = _probe(ffmpeg_binary, path)
    return None if info is None else info[0]


def _probe(ffmpeg_binary: str, path: Path) -> tuple[float, str] | None:
    """Decode the file once, returning (duration, stderr) when it is healthy."""

    if not path.is_file() or path.stat().st_size == 0:
        return None
    try:
        result = subprocess.run(
            [ffmpeg_binary, "-hide_banner", "-i", str(path), "-f", "null", "-"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    text = result.stderr.decode("utf-8", "replace")
    # The summary line looks like: "  Duration: 00:00:04.00, start: 0.000000, ..."
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("Duration:"):
            continue
        raw = line.split("Duration:", 1)[1].split(",", 1)[0].strip()
        if raw.startswith("N/A"):
            return None
        try:
            hours, minutes, seconds = raw.split(":")
            duration = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
        except (ValueError, IndexError):
            return None
        return duration, text
    return None


def artifact_looks_complete(ffmpeg_binary: str, path: Path, *, min_seconds: float) -> bool:
    """A success is only accepted if it decodes and is roughly the expected length."""

    if not path.is_file() or path.stat().st_size < 1024:
        return False
    info = _probe(ffmpeg_binary, path)
    if info is None:
        return False
    duration, _stderr = info
    return duration >= max(0.25, min_seconds * 0.5)


def concat_clips(settings: Settings, artifacts: Iterable[Path], target: Path) -> Path:
    """Compose selected per-segment artifacts in order, stream-copying.

    All attempts share one encode spec, so concat needs no re-encode and can
    start even while unrelated cancelled processes are still winding down.
    """

    files = list(artifacts)
    if len(files) < len(settings.clip_names):
        raise MediaError("not all segments have a selected artifact")
    for path in files:
        if not path.is_file() or path.stat().st_size == 0:
            raise MediaError(f"missing artifact: {path}")

    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp.mp4")
    list_path: Path | None = None
    try:
        fd, raw_list_path = tempfile.mkstemp(
            prefix="concat_", suffix=".txt", dir=str(target.parent)
        )
        list_path = Path(raw_list_path)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            for path in files:
                # concat demuxer escaping: single quotes must be escaped.
                escaped = str(path.resolve()).replace("'", "'\\''")
                handle.write(f"file '{escaped}'\n")
        result = _run(
            settings.ffmpeg_binary,
            [
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(list_path),
                "-map",
                "0:v:0",
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                "-f",
                "mp4",
                str(tmp),
            ],
            timeout=120,
        )
        if result.returncode != 0 or not tmp.is_file() or tmp.stat().st_size == 0:
            detail = result.stderr.decode("utf-8", "replace").strip()[-800:]
            raise MediaError(f"composition failed: {detail}")
        os.replace(tmp, target)
        return target
    finally:
        tmp.unlink(missing_ok=True)
        if list_path is not None:
            list_path.unlink(missing_ok=True)


def terminate_process(proc: subprocess.Popen) -> None:
    """Ask a process to exit; escalation is handled by the attempt monitor."""

    if proc.poll() is not None:
        return
    try:
        proc.terminate()
    except (ProcessLookupError, PermissionError, OSError):
        # The process may have exited between poll() and the signal, or the
        # host may refuse signalling; the monitor re-checks liveness anyway.
        return


def kill_process(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        proc.kill()
    except (ProcessLookupError, PermissionError, OSError):
        return


def remove_tree(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)
