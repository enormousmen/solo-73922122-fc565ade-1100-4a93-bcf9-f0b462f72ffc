#!/usr/bin/env python3
"""Deterministic fake ffmpeg used only by the test-suite.

Behaviour is read from ``fake_config.json`` next to this script. The scheduler
invokes ffmpeg with a fixed argument shape, which lets this shim distinguish:

* artifact probing:  ... -i <file> -f null -
* transcoding:       ... [-readrate R] -i <src> ... -f mp4 <out>
* concat:            ... -f concat -i <list> ... -f mp4 <out>
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time
from pathlib import Path

CONFIG_PATH = Path(__file__).with_name("fake_config.json")
DEFAULT = {"sleep": 0.05, "exit": 0, "ignore_term": 0.0, "artifact": True}


def load_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def emit_duration(seconds: float) -> None:
    whole = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = seconds % 60
    sys.stderr.write(
        "Input #0, mov,mp4,m4a,3gp,3g2,mj2, from 'fake.mp4':\n"
        f"  Duration: {whole:02d}:{minutes:02d}:{secs:05.2f}, start: 0.000000, "
        "bitrate: 230 kb/s\n"
        "Stream #0:0: Video: h264, yuv420p, 640x360\n"
    )
    sys.stderr.flush()


def write_artifact(path: Path, size: int = 4096) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(b"\x00" * 8 + b"fake-mp4" * (size // 8 + 1))


def segment_for(output: Path) -> str:
    """Map an output filename to its clip letter.

    Handles both transcode targets (``A_<attempt>.mp4``) and fixture targets
    (``clip_1_A.mp4``).
    """

    stem = output.stem
    for part in reversed(stem.split("_")):
        if part in {"A", "B", "C"}:
            return part
    return "?"


def main(argv: list[str]) -> int:
    config = load_config()
    if "-i" in argv and "-f" in argv and argv[-1] == "-":
        emit_duration(float(config.get("probe_seconds", 4.0)))
        return int(config.get("probe_exit", 0))

    output = Path(argv[-1])
    run = config.get("segments", {}).get(segment_for(output), {})
    mode = "slow" if "-readrate" in argv else "normal"
    defaults = {k: v for k, v in config.items() if k != "segments"}
    behaviour = {**DEFAULT, **defaults, **run.get(mode, {})}

    terminated_at: list[float] = []

    def on_term(signum, _frame):  # noqa: ARG001
        terminated_at.append(time.monotonic())

    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)

    start = time.monotonic()
    deadline = start + float(behaviour["sleep"])
    ignore_term = float(behaviour["ignore_term"])

    while True:
        now = time.monotonic()
        if terminated_at:
            if ignore_term <= 0:
                output.unlink(missing_ok=True)
                sys.stderr.write("fake-ffmpeg: terminated\n")
                sys.stderr.flush()
                return 143
            if now - terminated_at[0] >= ignore_term:
                break  # produce a late artifact, then exit 0
        elif now >= deadline:
            break
        time.sleep(0.01)

    exit_code = int(behaviour["exit"])
    if exit_code != 0 or not behaviour["artifact"]:
        output.unlink(missing_ok=True)
        sys.stderr.write(f"fake-ffmpeg: simulated failure for {output.name}\n")
        sys.stderr.flush()
        return exit_code or 1

    write_artifact(output)
    return 0


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    sys.exit(main(sys.argv[1:]))
