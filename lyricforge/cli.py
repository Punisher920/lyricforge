"""Command line entry point for lyricforge."""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image

from . import ffmpeg, matte, render
from .matte import Rect
from .render import Canvas


@dataclass(frozen=True)
class Analysis:
    info: ffmpeg.VideoInfo
    scale: float
    card: Rect | None
    lyrics: Rect | None


def analyse(source: Path, samples: int = 12, block: int = matte.DEFAULT_BLOCK) -> Analysis:
    """Sample the song to find the overlay's contrast and its cover-art card."""
    info = ffmpeg.probe(source)
    rate = samples / info.duration if info.duration else 1.0

    frames: list[np.ndarray] = []
    for frame in ffmpeg.read_frames(source, width=info.width, height=info.height, fps=rate):
        frames.append(frame.astype(np.float32) / 255.0)
        if len(frames) >= samples:
            break
    if not frames:
        raise ffmpeg.FFmpegError(f"No frames decoded from {source}.")

    scale = float(np.median([matte.overlay_scale(f, block) for f in frames]))
    card = matte.find_card(frames, block=block)
    lyrics = matte.find_lyrics_band(frames, block=block)
    return Analysis(info=info, scale=scale, card=card,
                    lyrics=_clip_below(lyrics, card))


def _clip_below(band: Rect | None, card: Rect | None) -> Rect | None:
    """Pull the band's top below the card.

    The band is padded by a margin to avoid clipping tall glyphs, which can
    reach back over the bottom edge of the cover art and drag a sliver of it
    into a lyrics-only render.
    """
    if band is None or card is None:
        return band
    card_bottom = card.y + card.height
    if band.y >= card_bottom or card_bottom >= band.y + band.height:
        return band
    return Rect(band.x, card_bottom, band.width,
                band.height - (card_bottom - band.y))


def _resolve_card(choice: str, detected: Rect | None, lyrics_only: bool) -> Rect | None:
    if choice == "none":
        return None
    if choice == "auto":
        # Keeping only the lyrics means the card goes too, unless asked for.
        return None if lyrics_only else detected
    return Rect.parse(choice)


def _resolve_keep(args: argparse.Namespace, found: Analysis) -> tuple[Rect, ...]:
    if args.keep:
        return tuple(args.keep)
    if not args.only_lyrics:
        return ()
    if found.lyrics is None:
        raise ValueError(
            "Could not find a lyrics band; pass --keep X,Y,W,H explicitly."
        )
    return (found.lyrics,)


def cmd_inspect(args: argparse.Namespace) -> int:
    result = analyse(args.source, samples=args.samples, block=args.block)
    info = result.info

    print(f"{args.source.name}")
    print(f"  {info.width}x{info.height} @ {info.fps:g}fps"
          f"{f', {info.duration:.1f}s' if info.duration else ''}"
          f"{', audio' if info.has_audio else ', NO AUDIO'}")
    print(f"  overlay contrast : {result.scale:.4f}")
    print(f"  cover-art card   : {result.card or 'not detected'}")
    print(f"  lyrics band      : {result.lyrics or 'not detected'}")

    if args.preview:
        frame = ffmpeg.read_single_frame(
            args.source, width=info.width, height=info.height,
            filters=[f"select='gte(t,{args.at})'"],
        ).astype(np.float32) / 255.0
        colour, alpha = matte.extract(
            frame, result.scale, block=args.block, gate=args.gate,
            opaque=_resolve_card(args.card, result.card, args.only_lyrics),
            keep=_resolve_keep(args, result),
            drop=tuple(args.drop),
        )
        checker = _checkerboard(info.width, info.height)
        composed = alpha[..., None] * colour + (1 - alpha[..., None]) * checker
        Image.fromarray((composed * 255).clip(0, 255).astype(np.uint8)).save(args.preview)
        print(f"  preview written  : {args.preview}")
    return 0


def _checkerboard(width: int, height: int, size: int = 32) -> np.ndarray:
    """Transparency checkerboard, so holes in the matte are obvious."""
    ys, xs = np.mgrid[0:height, 0:width]
    tiles = ((xs // size) + (ys // size)) % 2
    return np.where(tiles[..., None] == 0, 0.6, 0.4).astype(np.float32)


def cmd_render(args: argparse.Namespace) -> int:
    result = analyse(args.source, samples=args.samples, block=args.block)
    card = _resolve_card(args.card, result.card, args.only_lyrics)
    keep = _resolve_keep(args, result)

    print(f"source   : {args.source.name} "
          f"({result.info.width}x{result.info.height} @ {result.info.fps:g}fps)")
    print(f"card     : {card or 'none'}")
    print(f"keep     : {', '.join(str(r) for r in keep) or 'whole frame'}")
    print(f"backdrop : {args.background.name}")

    outputs = _output_paths(args)
    for ratio, destination in outputs:
        canvas = Canvas.from_ratio(ratio, args.short_edge)
        print(f"\n-> {ratio} {canvas.width}x{canvas.height} : {destination}")
        render.render(
            args.source, args.background, destination,
            canvas=canvas, fps=args.fps, scale=result.scale,
            opaque=card, keep=keep, drop=tuple(args.drop),
            block=args.block, gate=args.gate,
            zoom=args.zoom, crf=args.crf, preset=args.preset,
            limit_seconds=args.duration,
        )
    print(f"\nDone: {len(outputs)} file(s).")
    return 0


def _output_paths(args: argparse.Namespace) -> list[tuple[str, Path]]:
    if args.out and len(args.ratio) == 1:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        return [(args.ratio[0], args.out)]
    if args.out and len(args.ratio) > 1:
        raise SystemExit("--out takes a single --ratio; use --out-dir for several.")

    directory = args.out_dir or Path.cwd()
    directory.mkdir(parents=True, exist_ok=True)
    stem = args.source.stem
    return [(r, directory / f"{stem}_{r.replace(':', 'x')}.mp4") for r in args.ratio]


def cmd_ui(args: argparse.Namespace) -> int:
    from .server import serve

    serve(port=args.port, open_browser=not args.no_browser)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lyricforge",
        description="Lift Suno's lyric overlay off its gradient and drop it "
                    "onto your own looping background.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def shared(p: argparse.ArgumentParser) -> None:
        p.add_argument("source", type=Path, help="Suno lyric video (.mp4)")
        p.add_argument("--card", default="auto",
                       help="Cover-art card: 'auto', 'none', or 'x,y,w,h'")
        p.add_argument("--keep", type=Rect.parse, action="append", default=[],
                       metavar="X,Y,W,H",
                       help="Keep only this region; repeatable")
        p.add_argument("--only-lyrics", action="store_true",
                       help="Keep just the scrolling lyrics: drops the title, "
                            "byline, cover art and watermark")
        p.add_argument("--drop", type=Rect.parse, action="append", default=[],
                       metavar="X,Y,W,H", help="Region to erase; repeatable")
        p.add_argument("--block", type=int, default=matte.DEFAULT_BLOCK,
                       help="Background estimation block size")
        p.add_argument("--gate", type=float, default=matte.DEFAULT_GATE,
                       help="Alpha below this is treated as noise")
        p.add_argument("--samples", type=int, default=12,
                       help="Frames sampled when analysing")

    inspect = sub.add_parser("inspect", help="Report layout without rendering")
    shared(inspect)
    inspect.add_argument("--preview", type=Path, help="Write a cut-out preview PNG")
    inspect.add_argument("--at", type=float, default=30.0,
                         help="Timestamp for the preview, in seconds")
    inspect.set_defaults(func=cmd_inspect)

    render_cmd = sub.add_parser("render", help="Composite onto a new background")
    shared(render_cmd)
    render_cmd.add_argument("--background", type=Path, required=True,
                            help="Background clip; looped to cover the song")
    render_cmd.add_argument("--ratio", action="append", default=None,
                            help="Output ratio, e.g. 9:16; repeatable")
    render_cmd.add_argument("-o", "--out", type=Path, help="Output file (single ratio)")
    render_cmd.add_argument("--out-dir", type=Path, help="Directory for several ratios")
    render_cmd.add_argument("--fps", type=float, default=30.0, help="Output frame rate")
    render_cmd.add_argument("--short-edge", type=int, default=render.DEFAULT_SHORT_EDGE,
                            help="Short edge of the output in pixels")
    render_cmd.add_argument("--zoom", type=float, default=1.0,
                            help="Scale the overlay within the canvas")
    render_cmd.add_argument("--duration", type=float,
                            help="Render only the first N seconds (for previewing)")
    render_cmd.add_argument("--crf", type=int, default=18, help="x264 quality (lower=better)")
    render_cmd.add_argument("--preset", default="medium", help="x264 speed preset")
    render_cmd.set_defaults(func=cmd_render)

    ui = sub.add_parser("ui", help="Open the drag-and-drop window")
    ui.add_argument("--port", type=int, default=8765)
    ui.add_argument("--no-browser", action="store_true",
                    help="Do not open a browser automatically")
    ui.set_defaults(func=cmd_ui)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if getattr(args, "ratio", None) is None and args.command == "render":
        args.ratio = ["9:16"]
    try:
        return args.func(args)
    except (ffmpeg.FFmpegError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
