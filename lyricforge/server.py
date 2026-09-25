"""A small local web UI for people who would rather not use a terminal.

Binds to localhost only and runs entirely on the standard library. Uploads
arrive as raw request bodies with the name in a header, which avoids parsing
multipart forms.
"""

from __future__ import annotations

import json
import re
import shutil
import threading
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from . import matte, render
from .matte import Rect
from .render import Canvas

WEB_ROOT = Path(__file__).parent / "web"
MAX_UPLOAD = 2 * 1024 * 1024 * 1024  # 2 GB
SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")

_jobs: dict[str, dict[str, Any]] = {}
_lock = threading.Lock()


def _safe_name(raw: str) -> str:
    name = SAFE_NAME.sub("_", Path(raw).name).strip("._") or "upload"
    return name[:120]


def _update(job_id: str, **fields: Any) -> None:
    with _lock:
        _jobs.setdefault(job_id, {}).update(fields)


def _snapshot(job_id: str) -> dict[str, Any] | None:
    with _lock:
        job = _jobs.get(job_id)
        return dict(job) if job else None


def _keep_regions(analysis, only_lyrics: bool) -> tuple[Rect, ...]:
    if not only_lyrics:
        return ()
    if analysis.lyrics is None:
        raise ValueError(
            "Could not find a lyrics band in this video. Render it with the "
            "whole overlay instead, or set the region by hand on the "
            "command line with --keep."
        )
    return (analysis.lyrics,)


def _run_render(job_id: str, work: Path, params: dict[str, Any]) -> None:
    """Render every requested ratio, recording progress as it goes."""
    from .cli import analyse  # imported late; cli pulls in argparse

    try:
        song = work / params["song"]
        background = work / params["background"]
        only_lyrics = bool(params.get("onlyLyrics", True))
        duration = params.get("duration") or None
        ratios = params.get("ratios") or ["9:16"]

        _update(job_id, stage="Analysing the song", percent=0)
        found = analyse(song)
        keep = _keep_regions(found, only_lyrics)
        card = None if only_lyrics else found.card

        outputs = []
        for index, ratio in enumerate(ratios):
            canvas = Canvas.from_ratio(ratio)
            destination = work / f"{song.stem}_{ratio.replace(':', 'x')}.mp4"

            def report(done: int, total: int | None, _r=ratio, _i=index) -> None:
                share = (done / total) if total else 0.0
                overall = (_i + min(share, 1.0)) / len(ratios)
                _update(job_id, stage=f"Rendering {_r}", percent=round(100 * overall))

            render.render(
                song, background, destination,
                canvas=canvas, fps=float(params.get("fps", 30)),
                scale=found.scale, opaque=card, keep=keep,
                zoom=float(params.get("size", 1.0)),
                shade=float(params.get("shade", render.DEFAULT_SHADE)),
                limit_seconds=duration, progress=False, on_progress=report,
            )
            outputs.append({"ratio": ratio, "name": destination.name,
                            "size": destination.stat().st_size})

        _update(job_id, stage="Done", percent=100, state="done", outputs=outputs)
    except Exception as exc:  # surfaced to the page rather than the console
        _update(job_id, state="error", error=str(exc), stage="Failed")


class Handler(BaseHTTPRequestHandler):
    server_version = "lyricforge"
    work: Path = Path(".")

    def log_message(self, *args: Any) -> None:  # quieter console
        pass

    # -- helpers -------------------------------------------------------
    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: dict[str, Any], code: int = 200) -> None:
        self._send(code, json.dumps(payload).encode(), "application/json")

    def _fail(self, message: str, code: int = 400) -> None:
        self._json({"error": message}, code)

    def _body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_UPLOAD:
            raise ValueError("File is too large.")
        return self.rfile.read(length)

    def _in_work(self, name: str) -> Path | None:
        """Resolve a name inside the work directory, refusing anything else."""
        candidate = (self.work / Path(name).name).resolve()
        if candidate.parent != self.work.resolve() or not candidate.exists():
            return None
        return candidate

    # -- routes --------------------------------------------------------
    def do_GET(self) -> None:
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            page = (WEB_ROOT / "index.html").read_bytes()
            return self._send(200, page, "text/html; charset=utf-8")

        if path.startswith("/api/job/"):
            job = _snapshot(path.rsplit("/", 1)[-1])
            return self._json(job) if job else self._fail("No such job.", 404)

        if path.startswith("/api/file/"):
            target = self._in_work(path.rsplit("/", 1)[-1])
            if target is None:
                return self._fail("Not found.", 404)
            kind = "image/png" if target.suffix == ".png" else "video/mp4"
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(target.stat().st_size))
            self.send_header("Content-Disposition",
                             f'attachment; filename="{target.name}"')
            self.end_headers()
            with target.open("rb") as handle:
                shutil.copyfileobj(handle, self.wfile)
            return

        self._fail("Not found.", 404)

    def do_POST(self) -> None:
        path = self.path.split("?")[0]
        try:
            if path == "/api/upload":
                name = _safe_name(self.headers.get("X-Filename", "upload"))
                destination = self.work / name
                destination.write_bytes(self._body())
                return self._json({"name": name, "size": destination.stat().st_size})

            if path == "/api/inspect":
                return self._inspect(json.loads(self._body() or b"{}"))

            if path == "/api/render":
                params = json.loads(self._body() or b"{}")
                job_id = uuid.uuid4().hex[:12]
                _update(job_id, state="running", stage="Starting", percent=0)
                threading.Thread(
                    target=_run_render, args=(job_id, self.work, params), daemon=True
                ).start()
                return self._json({"job": job_id})
        except Exception as exc:
            return self._fail(str(exc), 500)

        self._fail("Not found.", 404)

    def _inspect(self, params: dict[str, Any]) -> None:
        from .cli import analyse

        song = self._in_work(params.get("song", ""))
        if song is None:
            return self._fail("Upload a song first.")

        found = analyse(song)
        only_lyrics = bool(params.get("onlyLyrics", True))
        keep = _keep_regions(found, only_lyrics)

        from .ffmpeg import read_single_frame

        at = float(params.get("at", 30))
        frame = read_single_frame(
            song, width=found.info.width, height=found.info.height,
            filters=[f"select='gte(t,{at})'"],
        ).astype(np.float32) / 255.0
        colour, alpha = matte.extract(
            frame, found.scale,
            opaque=None if only_lyrics else found.card, keep=keep,
        )

        height, width = alpha.shape
        ys, xs = np.mgrid[0:height, 0:width]
        checker = np.where((((xs // 32) + (ys // 32)) % 2)[..., None] == 0,
                           0.6, 0.4).astype(np.float32)
        composed = alpha[..., None] * colour + (1 - alpha[..., None]) * checker

        preview = self.work / f"preview_{song.stem}.png"
        Image.fromarray((composed * 255).clip(0, 255).astype(np.uint8)).save(preview)

        self._json({
            "width": found.info.width, "height": found.info.height,
            "fps": found.info.fps, "duration": found.info.duration,
            "hasAudio": found.info.has_audio,
            "card": str(found.card) if found.card else None,
            "lyrics": str(found.lyrics) if found.lyrics else None,
            "preview": preview.name,
        })


def serve(host: str = "127.0.0.1", port: int = 8765,
          work: Path | None = None, open_browser: bool = True) -> None:
    Handler.work = (work or Path.home() / ".lyricforge" / "work").resolve()
    Handler.work.mkdir(parents=True, exist_ok=True)

    httpd = ThreadingHTTPServer((host, port), Handler)
    url = f"http://{host}:{port}/"
    print(f"lyricforge is running at {url}")
    print(f"Files are kept in {Handler.work}")
    print("Press Ctrl+C to stop.")
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        httpd.server_close()
