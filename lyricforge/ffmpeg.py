"""Locating ffmpeg/ffprobe and streaming raw frames through them.

Frames are moved as rgb24 over pipes so the rest of the tool can stay in
numpy and never hold a whole video in memory.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Sequence

import numpy as np

PIX_FMT = "rgb24"
CHANNELS = 3


class FFmpegError(RuntimeError):
    """ffmpeg is missing, or exited non-zero."""


def _bundled_ffmpeg() -> str | None:
    """The static build that ships with imageio-ffmpeg, if installed."""
    try:
        import imageio_ffmpeg
    except ImportError:
        return None
    try:
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def find_ffmpeg() -> str:
    exe = shutil.which("ffmpeg") or _bundled_ffmpeg()
    if exe:
        return exe
    raise FFmpegError(
        "ffmpeg not found. Install it from https://ffmpeg.org/download.html, "
        "or run `pip install imageio-ffmpeg` for a bundled build."
    )


def find_ffprobe() -> str | None:
    return shutil.which("ffprobe")


@dataclass(frozen=True)
class VideoInfo:
    width: int
    height: int
    fps: float
    duration: float | None
    has_audio: bool

    @property
    def frame_bytes(self) -> int:
        return self.width * self.height * CHANNELS


def probe(path: Path | str) -> VideoInfo:
    """Read geometry, frame rate and stream layout from a media file."""
    ffprobe = find_ffprobe()
    if ffprobe:
        return _probe_ffprobe(ffprobe, path)
    return _probe_ffmpeg(path)


def _probe_ffprobe(ffprobe: str, path: Path | str) -> VideoInfo:
    cmd = [ffprobe, "-v", "error", "-show_streams", "-show_format",
           "-of", "json", str(path)]
    out = subprocess.run(cmd, capture_output=True, text=True)
    if out.returncode != 0:
        raise FFmpegError(f"ffprobe failed on {path}:\n{out.stderr.strip()}")
    data = json.loads(out.stdout)
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        raise FFmpegError(f"{path} has no video stream.")

    duration = None
    for source in (video.get("duration"), data.get("format", {}).get("duration")):
        try:
            duration = float(source)
            break
        except (TypeError, ValueError):
            continue

    return VideoInfo(
        width=int(video["width"]),
        height=int(video["height"]),
        fps=_parse_fraction(video.get("avg_frame_rate") or video.get("r_frame_rate")),
        duration=duration,
        has_audio=any(s.get("codec_type") == "audio" for s in streams),
    )


def _probe_ffmpeg(path: Path | str) -> VideoInfo:
    """Fallback that scrapes `ffmpeg -i` when ffprobe is not installed."""
    cmd = [find_ffmpeg(), "-hide_banner", "-i", str(path)]
    text = subprocess.run(cmd, capture_output=True, text=True).stderr

    size = re.search(r"Stream #\d+:\d+.*?Video:.*?(\d{2,5})x(\d{2,5})", text, re.S)
    if not size:
        raise FFmpegError(f"Could not read video geometry from {path}.")

    fps_match = re.search(r"(\d+(?:\.\d+)?)\s+fps", text)
    dur_match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", text)
    duration = None
    if dur_match:
        h, m, s = dur_match.groups()
        duration = int(h) * 3600 + int(m) * 60 + float(s)

    return VideoInfo(
        width=int(size.group(1)),
        height=int(size.group(2)),
        fps=float(fps_match.group(1)) if fps_match else 30.0,
        duration=duration,
        has_audio=bool(re.search(r"Stream #\d+:\d+.*?Audio:", text)),
    )


def _parse_fraction(value: str | None) -> float:
    if not value:
        return 30.0
    if "/" in value:
        num, _, den = value.partition("/")
        try:
            den_f = float(den)
            return float(num) / den_f if den_f else 30.0
        except ValueError:
            return 30.0
    try:
        return float(value)
    except ValueError:
        return 30.0


def read_frames(
    path: Path | str,
    *,
    width: int,
    height: int,
    filters: Sequence[str] = (),
    fps: float | None = None,
    loop: bool = False,
) -> Iterator[np.ndarray]:
    """Yield rgb24 frames as (height, width, 3) uint8 arrays.

    `width`/`height` are the dimensions *after* `filters` run, since that is
    what actually comes down the pipe. Set `loop` to repeat the input forever;
    the caller is then responsible for stopping.
    """
    chain = list(filters)
    if fps:
        chain.insert(0, f"fps={fps}")

    cmd = [find_ffmpeg(), "-v", "error"]
    if loop:
        cmd += ["-stream_loop", "-1"]
    cmd += ["-i", str(path)]
    if chain:
        cmd += ["-vf", ",".join(chain)]
    cmd += ["-an", "-f", "rawvideo", "-pix_fmt", PIX_FMT, "-"]

    frame_bytes = width * height * CHANNELS
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert proc.stdout is not None
    try:
        while True:
            buf = proc.stdout.read(frame_bytes)
            if len(buf) < frame_bytes:
                break
            yield np.frombuffer(buf, np.uint8).reshape(height, width, CHANNELS)
    finally:
        _shutdown(proc)


def read_single_frame(
    path: Path | str, *, width: int, height: int, filters: Sequence[str] = ()
) -> np.ndarray:
    """Run one image or frame through a filter chain and return it."""
    for frame in read_frames(path, width=width, height=height, filters=filters):
        return frame.copy()
    raise FFmpegError(f"No frame decoded from {path}.")


def open_writer(
    out_path: Path | str,
    *,
    width: int,
    height: int,
    fps: float,
    audio_from: Path | str | None = None,
    crf: int = 18,
    preset: str = "medium",
) -> subprocess.Popen:
    """Start an encoder that accepts rgb24 frames on stdin."""
    cmd = [
        find_ffmpeg(), "-v", "error", "-y",
        "-f", "rawvideo", "-pix_fmt", PIX_FMT,
        "-s", f"{width}x{height}", "-r", f"{fps}", "-i", "-",
    ]
    if audio_from is not None:
        cmd += ["-i", str(audio_from), "-map", "0:v:0", "-map", "1:a:0",
                "-c:a", "aac", "-b:a", "320k", "-shortest"]
    cmd += [
        "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
        "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out_path),
    ]
    return subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)


def close_writer(proc: subprocess.Popen) -> None:
    if proc.stdin:
        proc.stdin.close()
    stderr = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
    if proc.wait() != 0:
        raise FFmpegError(f"Encoding failed:\n{stderr.strip()}")


def _shutdown(proc: subprocess.Popen) -> None:
    """Stop a decoder we may have abandoned mid-stream."""
    if proc.poll() is None:
        proc.kill()
    for stream in (proc.stdout, proc.stderr):
        if stream:
            stream.close()
    proc.wait()
