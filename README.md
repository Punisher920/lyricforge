# lyricforge

Lift Suno's lyric overlay off its gradient background and composite it onto
your own looping video — keeping Suno's timing, so nothing needs re-syncing.

Generate a background once, reuse it across every song. No per-song
generation credits.

## Why this works

Suno renders lyric videos as text over a slowly morphing colour gradient.
That gradient is **animated**, so subtracting a single background plate
doesn't work. But it carries no fine detail, while text is all fine detail.

So the background is estimated *per frame* by taking the median of each 32x32
block of pixels: text is a minority inside any block, so the median lands on
the gradient underneath. Whatever is left after subtracting that estimate is
the overlay. Measured on a real export, that separates overlay from
background by 60-100x.

The alpha is then unpremultiplied against the estimated background
(`colour = background + residual / alpha`), which is what stops the old purple
gradient from haloing antialiased letter edges over your new background.

One special case: Suno's cover-art card is a photograph, and its flat fills
look like background to the block median. The card sits at a fixed position,
so it is detected once and passed through at full opacity instead.

## Install

Needs Python 3.10 or newer. Nothing else — ffmpeg comes bundled.

```bash
pip install -r requirements.txt
```

A system ffmpeg is used when you have one, because it is faster and brings
`ffprobe` with it. Without one the bundled build is used and geometry is read
by parsing ffmpeg's own output instead.

## Just double-click it

**Windows** — double-click `lyricforge.bat`
**macOS** — double-click `lyricforge.command`

The first run sets up a private Python environment inside the project folder
and installs what it needs, which takes a minute or two. Every run after that
starts in about a second. Your browser opens on its own; leave the black
console window open while you work.

If it reports that Python is missing, install Python 3.10 or newer from
<https://www.python.org/downloads/> and, on Windows, tick **Add python.exe to
PATH** on the installer's first screen.

## The window (manual)

```bash
pip install -r requirements.txt
python -m lyricforge ui
```

Your browser opens. Drag in your Suno video and a background clip, tick the
platforms you want, press **Check first**, then **Render**. Download links
appear when it finishes.

It serves on `127.0.0.1` only, so nothing is exposed to your network, and
working files stay in `~/.lyricforge/work`.

## Quickstart (command line)

Put your song and your background clip in this folder, then:

```bash
# 1. Look before you render. Costs seconds, writes a PNG you can eyeball.
python -m lyricforge inspect mysong.mp4 --only-lyrics --preview check.png

# 2. Try 15 seconds.
python -m lyricforge render mysong.mp4 --background loop.mp4 \
    --only-lyrics --ratio 9:16 -o test.mp4 --duration 15

# 3. Happy? Drop --duration and render every platform at once.
python -m lyricforge render mysong.mp4 --background loop.mp4 \
    --only-lyrics --ratio 9:16 --ratio 1:1 --ratio 16:9 --out-dir ./out
```

Step 1 is the important habit: `check.png` shows exactly what will be kept,
on a transparency checkerboard, before you spend minutes on a render.

## Use

Check what it found before rendering anything:

```bash
python -m lyricforge inspect SONG.mp4 --preview cutout.png --at 30
```

`cutout.png` shows the extracted overlay on a transparency checkerboard —
holes and halos are obvious there before you spend time on a full render.

Render onto your own background, several platforms at once:

```bash
python -m lyricforge render SONG.mp4 \
    --background loop.mp4 \
    --ratio 9:16 --ratio 1:1 --ratio 16:9 \
    --out-dir ./out
```

The background is looped to cover the whole song and cropped to fill each
canvas, so a single short clip covers a three-minute track.

Preview the first few seconds before committing to a full render:

```bash
python -m lyricforge render SONG.mp4 --background loop.mp4 --duration 10 -o test.mp4
```

### Lyrics only

To drop Suno's title, byline, cover art and watermark and keep just the
scrolling words:

```bash
python -m lyricforge render SONG.mp4 --background loop.mp4 --only-lyrics \
    --ratio 9:16 -o out.mp4
```

The band is found automatically. Title, byline, cover art and watermark are
painted into every frame; only the lyrics come and go. Counting per row how
many pixels are lit in *nearly all* samples against how many are lit in
*some* separates them — lyric rows have plenty of the second and almost none
of the first.

Check it with `inspect --only-lyrics --preview` before rendering. If a layout
fools it, set the region by hand with `--keep X,Y,W,H`, which overrides
detection.

### Options worth knowing

| Flag | Does |
|---|---|
| `--ratio` | Repeatable. `9:16`, `16:9`, `1:1`, `4:5`. Short edge is 1080 by default, so these give 1080x1920, 1920x1080, 1080x1080 and Instagram's 1080x1350. |
| `--only-lyrics` | Keep just the scrolling lyrics; drops title, byline, card and watermark. |
| `--keep X,Y,W,H` | Keep only this region, overriding detection. Repeatable. |
| `--card` | `auto` (default), `none`, or explicit `x,y,w,h` when detection is off. Ignored under `--only-lyrics` unless set explicitly. |
| `--drop X,Y,W,H` | Erase a region. Repeatable. |
| `--zoom` | Scale the overlay within the canvas. Above 1.0 it crops rather than letterboxing — see below. |
| `--duration` | Render only the first N seconds. |
| `--crf` / `--preset` | x264 quality and speed. |

## Known limitations

- **A 9:16 source letterboxes into 16:9.** Suno exports vertical, so a
  widescreen render puts the lyrics in a narrow centre column. `--zoom 2`
  fills the frame but crops the title and watermark off the top and bottom.
  For a proper widescreen lyric video you want the text re-laid-out, not
  rescaled.
- **The card may show a faint rectangular edge** where its drop shadow is
  passed through opaque. Tighten it with an explicit `--card x,y,w,h`.
- **A faint streak can survive just above the first lyric line** under
  `--only-lyrics`, from the soft shadow under the cover art. Nudge the band
  down with `--keep` if it shows against your background.
- **Speed**: roughly real-time per output at 1080p. A 3-minute song takes a
  few minutes per ratio.
- Suno's export is 10fps; output defaults to 30fps, so the background moves
  smoothly while the overlay updates at its original rate.
- `--drop` can remove any region including the Suno watermark; check your
  Suno plan's attribution terms before publishing without it.

## Layout

| File | Role |
|---|---|
| `lyricforge/ffmpeg.py` | Locating ffmpeg, probing files, streaming raw frames |
| `lyricforge/matte.py` | Background estimation, alpha solve, card and lyric-band detection |
| `lyricforge/render.py` | Canvas sizing, placement, streaming composite |
| `lyricforge/cli.py` | `inspect`, `render` and `ui` commands |
| `lyricforge/server.py` | Local web UI: uploads, jobs, progress, downloads |
| `lyricforge/web/index.html` | The page itself |
