"""Compositing an extracted lyric overlay onto a new looping background."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np
from PIL import Image

from . import ffmpeg, matte
from .matte import Rect

# Anchoring on the short edge gives 1920x1080, 1080x1920, 1080x1080 and
# Instagram's 1080x1350 from one rule.
DEFAULT_SHORT_EDGE = 1080


@dataclass(frozen=True)
class Canvas:
    width: int
    height: int

    @classmethod
    def from_ratio(cls, ratio: str, short_edge: int = DEFAULT_SHORT_EDGE) -> "Canvas":
        try:
            left, _, right = ratio.partition(":")
            num, den = float(left), float(right)
        except ValueError as exc:
            raise ValueError(f"Bad ratio {ratio!r}; expected e.g. '9:16'") from exc
        if num <= 0 or den <= 0:
            raise ValueError(f"Bad ratio {ratio!r}")
        if num >= den:
            width, height = short_edge * num / den, short_edge
        else:
            width, height = short_edge, short_edge * den / num
        return cls(_even(width), _even(height))


def _even(value: float) -> int:
    """H.264 needs even dimensions."""
    return max(2, int(round(value / 2)) * 2)


@dataclass(frozen=True)
class Placement:
    """Where a scaled overlay lands on the canvas, clipped to it.

    Zooming past 1.0 makes the overlay larger than the canvas, so both the
    destination and the source have to be clipped; without this the centring
    offsets go negative and wrap around the array.
    """

    width: int          # scaled overlay size
    height: int
    dest_x: int         # top-left of the visible part, on the canvas
    dest_y: int
    src_x: int          # matching top-left within the scaled overlay
    src_y: int
    copy_w: int         # size of the overlapping region
    copy_h: int


def _fit(source: tuple[int, int], canvas: Canvas, zoom: float) -> Placement:
    src_w, src_h = source
    factor = min(canvas.width / src_w, canvas.height / src_h) * zoom
    width, height = max(1, round(src_w * factor)), max(1, round(src_h * factor))

    left, top = (canvas.width - width) // 2, (canvas.height - height) // 2
    src_x, src_y = max(0, -left), max(0, -top)
    dest_x, dest_y = max(0, left), max(0, top)
    copy_w = min(width - src_x, canvas.width - dest_x)
    copy_h = min(height - src_y, canvas.height - dest_y)
    if copy_w <= 0 or copy_h <= 0:
        raise ValueError("Overlay does not overlap the canvas; check --zoom.")
    return Placement(width, height, dest_x, dest_y, src_x, src_y, copy_w, copy_h)


def _background_filters(canvas: Canvas) -> list[str]:
    """Fill the canvas with the background, cropping any overflow."""
    return [
        f"scale={canvas.width}:{canvas.height}:force_original_aspect_ratio=increase",
        f"crop={canvas.width}:{canvas.height}",
    ]


def render(
    source: Path,
    background: Path,
    out_path: Path,
    *,
    canvas: Canvas,
    fps: float,
    scale: float,
    opaque: Rect | None,
    keep: tuple[Rect, ...] = (),
    drop: tuple[Rect, ...] = (),
    block: int = matte.DEFAULT_BLOCK,
    gate: float = matte.DEFAULT_GATE,
    zoom: float = 1.0,
    crf: int = 18,
    preset: str = "medium",
    limit_seconds: float | None = None,
    progress: bool = True,
    on_progress: "Callable[[int, int | None], None] | None" = None,
) -> None:
    """Stream `source`'s overlay over a looping `background` into `out_path`."""
    info = ffmpeg.probe(source)
    src_w, src_h = info.width, info.height
    place = _fit((src_w, src_h), canvas, zoom)

    overlay_frames = ffmpeg.read_frames(source, width=src_w, height=src_h, fps=fps)
    background_frames = ffmpeg.read_frames(
        background, width=canvas.width, height=canvas.height,
        filters=_background_filters(canvas), fps=fps, loop=True,
    )

    writer = ffmpeg.open_writer(
        out_path, width=canvas.width, height=canvas.height, fps=fps,
        audio_from=source if info.has_audio else None, crf=crf, preset=preset,
    )

    total_seconds = info.duration
    if limit_seconds is not None:
        total_seconds = min(limit_seconds, total_seconds or limit_seconds)
    expected = int(total_seconds * fps) if total_seconds else None
    max_frames = int(limit_seconds * fps) if limit_seconds else None
    previous_raw: bytes | None = None
    cached: tuple[np.ndarray, np.ndarray] | None = None
    count = 0

    try:
        for raw_overlay, plate in zip(overlay_frames, background_frames):
            # Upsampling 10fps source to 30fps hands us each frame three
            # times; matte it once and reuse.
            key = raw_overlay.tobytes()
            if key != previous_raw or cached is None:
                frame = raw_overlay.astype(np.float32) / 255.0
                colour, alpha = matte.extract(
                    frame, scale, block=block, gate=gate,
                    opaque=opaque, keep=keep, drop=drop,
                )
                cached = _resize_overlay(colour, alpha, place.width, place.height)
                previous_raw = key

            writer.stdin.write(_composite(plate, cached, place))
            count += 1
            if on_progress is not None and count % 15 == 0:
                on_progress(count, expected)
            if max_frames and count >= max_frames:
                break
            if progress and count % 30 == 0:
                _report(count, expected)
    except BrokenPipeError as exc:
        raise ffmpeg.FFmpegError("Encoder closed the pipe early.") from exc
    finally:
        if progress:
            _report(count, expected, final=True)
        if on_progress is not None:
            on_progress(count, expected)
        ffmpeg.close_writer(writer)


def _resize_overlay(
    colour: np.ndarray, alpha: np.ndarray, width: int, height: int
) -> tuple[np.ndarray, np.ndarray]:
    """Scale colour and alpha to their placed size, as float32 0..1."""
    colour_img = Image.fromarray((colour * 255).astype(np.uint8))
    alpha_img = Image.fromarray((alpha * 255).astype(np.uint8), mode="L")
    size = (width, height)
    return (
        np.asarray(colour_img.resize(size, Image.BILINEAR), np.float32) / 255.0,
        np.asarray(alpha_img.resize(size, Image.BILINEAR), np.float32)[..., None] / 255.0,
    )


def _composite(
    plate: np.ndarray,
    overlay: tuple[np.ndarray, np.ndarray],
    place: Placement,
) -> bytes:
    colour, alpha = overlay
    out = plate.astype(np.float32) / 255.0

    src = (slice(place.src_y, place.src_y + place.copy_h),
           slice(place.src_x, place.src_x + place.copy_w))
    dest = (slice(place.dest_y, place.dest_y + place.copy_h),
            slice(place.dest_x, place.dest_x + place.copy_w))

    region = out[dest]
    out[dest] = alpha[src] * colour[src] + (1 - alpha[src]) * region
    return (out * 255).clip(0, 255).astype(np.uint8).tobytes()


def _report(count: int, expected: int | None, final: bool = False) -> None:
    if expected:
        line = f"\r  {count}/{expected} frames ({100 * count / expected:5.1f}%)"
    else:
        line = f"\r  {count} frames"
    sys.stderr.write(line + ("\n" if final else ""))
    sys.stderr.flush()
