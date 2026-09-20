"""Separating Suno's lyric overlay from its animated gradient background.

Suno renders lyric videos as text over a slowly morphing colour gradient. The
gradient carries no detail, so it can be estimated per frame by taking the
median of each block of pixels: text is a minority inside any given block, so
the median lands on the gradient underneath it. Whatever is left over after
subtracting that estimate is the overlay.

Because the background estimate is per frame, this works on an animated
gradient — a temporal median across frames would not.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from PIL import Image

# Blocks must be wider than a glyph stroke or the median follows the text.
DEFAULT_BLOCK = 32
# Alpha below this is compression noise in flat areas, not overlay.
DEFAULT_GATE = 0.08


@dataclass(frozen=True)
class Rect:
    """An axis-aligned region in source-pixel coordinates."""

    x: int
    y: int
    width: int
    height: int

    @property
    def slices(self) -> tuple[slice, slice]:
        return slice(self.y, self.y + self.height), slice(self.x, self.x + self.width)

    def scaled(self, factor: float) -> "Rect":
        return Rect(*(int(round(v * factor)) for v in
                      (self.x, self.y, self.width, self.height)))

    @classmethod
    def parse(cls, text: str) -> "Rect":
        parts = [p.strip() for p in text.split(",")]
        if len(parts) != 4:
            raise ValueError(f"Expected 'x,y,width,height', got {text!r}")
        return cls(*(int(p) for p in parts))

    def __str__(self) -> str:
        return f"{self.x},{self.y},{self.width},{self.height}"


def estimate_background(frame: np.ndarray, block: int = DEFAULT_BLOCK) -> np.ndarray:
    """Estimate the smooth gradient sitting behind the overlay.

    `frame` is float32 in 0..1. Returns an array of the same shape.
    """
    height, width, channels = frame.shape
    pad_h, pad_w = (-height) % block, (-width) % block
    padded = np.pad(frame, ((0, pad_h), (0, pad_w), (0, 0)), mode="edge")
    tall, wide = padded.shape[:2]

    tiles = padded.reshape(tall // block, block, wide // block, block, channels)
    small = np.median(tiles, axis=(1, 3))

    coarse = Image.fromarray((small * 255).clip(0, 255).astype(np.uint8))
    smooth = coarse.resize((wide, tall), Image.BICUBIC)
    return np.asarray(smooth, np.float32)[:height, :width] / 255.0


def overlay_scale(frame: np.ndarray, block: int = DEFAULT_BLOCK) -> float:
    """How far the overlay departs from the background, for normalising alpha."""
    residual = np.abs(frame - estimate_background(frame, block)).max(axis=-1)
    return float(np.percentile(residual, 99.5))


def extract(
    frame: np.ndarray,
    scale: float,
    *,
    block: int = DEFAULT_BLOCK,
    gate: float = DEFAULT_GATE,
    opaque: Rect | None = None,
    keep: tuple[Rect, ...] = (),
    drop: tuple[Rect, ...] = (),
) -> tuple[np.ndarray, np.ndarray]:
    """Split a frame into (colour, alpha).

    `opaque` is passed through at full alpha — used for Suno's cover-art card,
    whose flat fills would otherwise be mistaken for background. When `keep`
    is given, everything outside those regions is discarded. Regions in `drop`
    are forced transparent, and win over both.

    Returns colour as float32 0..1 (height, width, 3) and alpha as float32
    0..1 (height, width), unpremultiplied so they composite without haloing.
    """
    background = estimate_background(frame, block)
    residual = frame - background

    alpha = np.clip(np.abs(residual).max(axis=-1) / max(scale, 1e-6), 0.0, 1.0)
    alpha[alpha < gate] = 0.0

    # frame = alpha*colour + (1-alpha)*background, so colour = background +
    # residual/alpha. Recovering it here is what keeps the old gradient from
    # bleeding into antialiased edges over the new background.
    safe = np.maximum(alpha, 1e-3)[..., None]
    colour = np.clip(background + residual / safe, 0.0, 1.0)

    if opaque is not None:
        rows, cols = opaque.slices
        alpha[rows, cols] = 1.0
        colour[rows, cols] = frame[rows, cols]

    if keep:
        wanted = np.zeros(alpha.shape, dtype=bool)
        for rect in keep:
            wanted[rect.slices] = True
        alpha[~wanted] = 0.0

    for rect in drop:
        rows, cols = rect.slices
        alpha[rows, cols] = 0.0

    return colour, alpha


def find_card(
    frames: list[np.ndarray],
    *,
    block: int = DEFAULT_BLOCK,
    min_area_fraction: float = 0.01,
) -> Rect | None:
    """Locate Suno's cover-art card.

    The card is the one part of the overlay that is both static across the
    whole song and densely detailed; lyrics move, and text is sparse strokes.
    Returns None when nothing matches, which is the right answer for a layout
    that has no card.
    """
    if not frames:
        return None

    scale = float(np.median([overlay_scale(f, block) for f in frames]))
    always_on = None
    for frame in frames:
        residual = np.abs(frame - estimate_background(frame, block)).max(axis=-1)
        present = (residual / max(scale, 1e-6)) > DEFAULT_GATE
        always_on = present if always_on is None else (always_on & present)

    if always_on is None or not always_on.any():
        return None

    # Photographic fill stays lit under a box blur; glyph strokes wash out.
    density = _box_blur(always_on.astype(np.float32), radius=block // 2)
    dense = density > 0.6
    if not dense.any():
        return None

    rows = np.flatnonzero(dense.any(axis=1))
    cols = np.flatnonzero(dense.any(axis=0))
    rect = Rect(int(cols[0]), int(rows[0]),
                int(cols[-1] - cols[0] + 1), int(rows[-1] - rows[0] + 1))

    frame_area = always_on.shape[0] * always_on.shape[1]
    if rect.width * rect.height < min_area_fraction * frame_area:
        return None
    return rect


def _box_blur(values: np.ndarray, radius: int) -> np.ndarray:
    """Mean over a (2*radius+1) square, via summed-area table."""
    if radius < 1:
        return values
    padded = np.pad(values, radius + 1, mode="constant")
    table = padded.cumsum(axis=0).cumsum(axis=1)
    size = 2 * radius + 1
    height, width = values.shape
    total = (table[size:size + height, size:size + width]
             - table[0:height, size:size + width]
             - table[size:size + height, 0:width]
             + table[0:height, 0:width])
    return total / (size * size)


def _presence(
    frames: list[np.ndarray], block: int, gate: float
) -> tuple[np.ndarray, int]:
    """Count, per pixel, how many sampled frames show overlay there."""
    scale = float(np.median([overlay_scale(f, block) for f in frames]))
    counts = np.zeros(frames[0].shape[:2], dtype=np.int32)
    for frame in frames:
        residual = np.abs(frame - estimate_background(frame, block)).max(axis=-1)
        counts += (residual / max(scale, 1e-6) > gate).astype(np.int32)
    return counts, len(frames)


def find_lyrics_band(
    frames: list[np.ndarray],
    *,
    block: int = DEFAULT_BLOCK,
    gate: float = DEFAULT_GATE,
    margin: int = 12,
    max_gap: int | None = None,
) -> Rect | None:
    """Find the horizontal band holding the scrolling lyrics.

    Title, byline, cover art and watermark are painted in every frame, so
    their pixels are lit in nearly all samples. Lyrics come and go, so theirs
    are lit in a middling fraction. Counting "always on" against "sometimes
    on" per row separates them cleanly; a row is lyrics when it has plenty of
    the latter and little of the former.

    Edge flicker alone is not enough to qualify, which is what keeps the
    title — whose glyph edges shimmer as the gradient moves behind them —
    from being mistaken for lyrics.

    `max_gap` bridges the blank rows between lyric lines so they read as one
    band. It defaults to 4% of frame height, which on Suno's layout sits
    between the spacing of adjacent lyric lines and the larger gap down to
    the watermark. Override it if a layout packs things differently.
    """
    if not frames:
        return None

    counts, total = _presence(frames, block, gate)
    height, width = counts.shape
    if max_gap is None:
        max_gap = max(24, round(height * 0.04))

    low, high = max(1, int(0.10 * total)), int(0.85 * total)
    changing = ((counts >= low) & (counts <= high)).sum(axis=1)
    static = (counts >= total - 1).sum(axis=1)

    is_lyric = (changing >= 0.04 * width) & (changing > 3 * static)
    rows = np.flatnonzero(is_lyric)
    if rows.size == 0:
        return None

    # Keep the longest run, so a stray qualifying row elsewhere cannot stretch
    # the band across the whole frame.
    breaks = np.flatnonzero(np.diff(rows) > max_gap)
    runs = np.split(rows, breaks + 1)
    best = max(runs, key=len)

    top = max(0, int(best[0]) - margin)
    bottom = min(height - 1, int(best[-1]) + margin)
    return Rect(0, top, width, bottom - top + 1)
