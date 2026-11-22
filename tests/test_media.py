"""Media helpers: spec consistency, fixture generation, concat escaping."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from app import media
from app.config import Settings


def _ffmpeg() -> str | None:
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


FFMPEG = _ffmpeg()


def test_all_attempts_share_one_encode_spec(tmp_path):
    settings = Settings()
    settings.ffmpeg_binary = "ffmpeg"
    source = tmp_path / "in.mp4"

    normal = media.transcode_command(settings, source, target := tmp_path / "n.mp4", slow=False)
    slow = media.transcode_command(settings, source, target, slow=True)

    assert "-readrate" not in normal
    assert slow[slow.index("-readrate") + 1] == str(settings.slow_readrate)
    # Everything except the optional read-rate pair must be identical.
    strip = lambda cmd: [a for a in cmd if a not in ("-readrate", str(settings.slow_readrate))]
    assert strip(normal) == strip(slow)
    assert "-an" in normal
    assert normal[normal.index("-vf") + 1] == media.FILTER_CHAIN
    assert normal[normal.index("-c:v") + 1] == "libx264"


def test_filter_chain_normalises_size_fps_and_pixel_format():
    chain = media.FILTER_CHAIN
    assert "scale=640:360" in chain
    assert "pad=640:360" in chain
    assert "setsar=1" in chain
    assert "fps=30" in chain
    assert "format=yuv420p" in chain


@pytest.mark.skipif(FFMPEG is None, reason="real ffmpeg not available")
def test_fixtures_are_created_once_and_silent(tmp_path):
    settings = Settings()
    settings.data_dir = tmp_path / "data"
    settings.ffmpeg_binary = FFMPEG
    settings.clip_seconds = 1
    settings.input_dir.mkdir(parents=True)

    paths = media.ensure_fixtures(settings)
    assert [p.name for p in paths] == ["clip_1_A.mp4", "clip_2_B.mp4", "clip_3_C.mp4"]
    for path in paths:
        assert path.is_file() and path.stat().st_size > 0
        probe = subprocess.run(
            [FFMPEG, "-hide_banner", "-i", str(path)],
            capture_output=True,
            timeout=30,
            check=False,
        )
        text = probe.stderr.decode()
        assert "Audio:" not in text
        assert "640x360" in text

    mtimes = {p: p.stat().st_mtime_ns for p in paths}
    media.ensure_fixtures(settings)
    assert {p: p.stat().st_mtime_ns for p in paths} == mtimes  # not regenerated


@pytest.mark.skipif(FFMPEG is None, reason="real ffmpeg not available")
def test_probe_duration_reads_fixture(tmp_path):
    settings = Settings()
    settings.data_dir = tmp_path / "data"
    settings.ffmpeg_binary = FFMPEG
    settings.clip_seconds = 2
    media.ensure_fixtures(settings)
    duration = media.probe_duration(FFMPEG, settings.input_dir / "clip_1_A.mp4")
    assert duration is not None and 1.8 <= duration <= 2.2
    assert media.probe_duration(FFMPEG, tmp_path / "missing.mp4") is None


def test_concat_escapes_single_quotes(tmp_path, monkeypatch):
    settings = Settings()
    settings.ffmpeg_binary = "ffmpeg"
    odd = tmp_path / "it's odd.mp4"
    odd.write_bytes(b"x")
    target = tmp_path / "out.mp4"
    captured: dict = {}

    def fake_run(binary, args, *, timeout=None):
        captured["args"] = args
        list_path = Path(args[args.index("-i") + 1])
        captured["list"] = list_path.read_text(encoding="utf-8")
        # The production code writes to a sibling .tmp.mp4 then renames it.
        out = Path(args[-1])
        out.write_bytes(b"composed")
        return subprocess.CompletedProcess(args, 0, b"", b"")

    monkeypatch.setattr(media, "_run", fake_run)
    media.concat_clips(settings, [odd, odd, odd], target)
    assert target.is_file()
    assert captured["list"].count("file '") == 3
    assert "it'\\''s odd.mp4" in captured["list"]
    assert "-c" in captured["args"] and "copy" in captured["args"]
