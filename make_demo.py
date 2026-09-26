"""
Turn a Remote Play recording (made with R in `bridge.py --xbox`) into a demo
video and GIF, with a caption bar showing what the controller did.

    python make_demo.py                               # newest recording
    python make_demo.py recordings/20260926-231500 --start 5 --end 35
    python make_demo.py --blur 0.02,0.02,0.3,0.08     # blur a region, as fractions of the video

Writes docs/demo/demo.mp4, docs/images/demo.gif and, for checking the
recording before publishing it, contact-sheet.jpg in the recording folder.
"""

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from recorder import RECORDINGS

HERE = Path(__file__).parent
FONT = "/System/Library/Fonts/Helvetica.ttc"
MOVE_EVENTS = ("TILT", "Hub level")  # frequent, so they only show when nothing else happened
HIDDEN_EVENTS = ("Motor A",)  # "trigger returns to rest" would hide the more interesting bow shot
CAPTION_SECONDS = 2.5


def load_font(size: int, bold: bool = False):
    try:
        return ImageFont.truetype(FONT, size, index=1 if bold else 0)
    except OSError:
        return ImageFont.load_default()


def video_area(frame: dict, image: Image.Image) -> tuple[int, int, int, int]:
    """The part of the screenshot that shows the game, without black bars."""
    scale = image.width / frame["viewport"][0]
    box = frame["box"]
    x, y, w, h = box["x"], box["y"] + frame["offset_top"], box["width"], box["height"]
    aspect = box["videoWidth"] / box["videoHeight"]
    if w / h > aspect:  # bars left and right
        shown = h * aspect
        x, w = x + (w - shown) / 2, shown
    else:               # bars above and below
        shown = w / aspect
        y, h = y + (h - shown) / 2, shown
    return round(x * scale), round(y * scale), round((x + w) * scale), round((y + h) * scale)


def caption_for(t: float, events: list[dict]) -> dict | None:
    recent = [e for e in events if t - CAPTION_SECONDS <= e["t"] <= t and not e["source"].startswith(HIDDEN_EVENTS)]
    actions = [e for e in recent if not e["source"].startswith(MOVE_EVENTS)]
    if actions:
        return actions[-1]
    moves = [e for e in recent if t - 1.0 <= e["t"]]
    return moves[-1] if moves else None


def nice(source: str) -> str:
    """ "FORCE SENSOR pressed" -> "Force sensor pressed", keeping "Motor A" and "Magenta"."""
    words = [w.lower() if w.isupper() and len(w) > 1 else w for w in source.split()]
    text = " ".join(words)
    return text[:1].upper() + text[1:]


def render(frame: dict, folder: Path, events: list[dict], width: int, blur: list, captions: bool) -> Image.Image:
    image = Image.open(folder / "frames" / frame["file"]).convert("RGB")
    game = image.crop(video_area(frame, image))
    height = round(game.height * width / game.width) // 2 * 2
    game = game.resize((width, height), Image.LANCZOS)
    for fx, fy, fw, fh in blur:
        region = (round(fx * width), round(fy * height), round((fx + fw) * width), round((fy + fh) * height))
        game.paste(game.crop(region).filter(ImageFilter.GaussianBlur(radius=width // 60)), region)
    if not captions:
        return game

    bar = round(width * 0.055) // 2 * 2
    out = Image.new("RGB", (width, height + bar), "#15171b")
    out.paste(game, (0, 0))
    draw = ImageDraw.Draw(out)
    event = caption_for(frame["t"], events)
    small, big = load_font(round(bar * 0.34)), load_font(round(bar * 0.42), bold=True)
    label = "LEGO SPIKE controller"
    draw.text((bar * 0.4, height + bar / 2), label, font=small, fill="#8a93a3", anchor="lm")
    if event:
        x = bar * 0.4 + draw.textlength(label, font=small) + bar * 0.8
        source = nice(event["source"])
        draw.text((x, height + bar / 2), source, font=small, fill="#6cb6ff", anchor="lm")
        x += draw.textlength(source, font=small) + bar * 0.4
        mid, size = height + bar / 2, bar * 0.14  # arrow drawn as a triangle: the font has no "→"
        draw.polygon([(x, mid - size), (x + size * 1.6, mid), (x, mid + size)], fill="#9be38f")
        draw.text((x + bar * 0.45, mid), event["action"], font=big, fill="#9be38f", anchor="lm")
    return out


def contact_sheet(images: list[Image.Image], path: Path, columns: int = 4) -> None:
    thumb_w = 480
    thumbs = [im.resize((thumb_w, round(im.height * thumb_w / im.width))) for im in images]
    rows = (len(thumbs) + columns - 1) // columns
    sheet = Image.new("RGB", (columns * thumb_w, rows * thumbs[0].height), "black")
    for i, thumb in enumerate(thumbs):
        sheet.paste(thumb, ((i % columns) * thumb_w, (i // columns) * thumb.height))
    sheet.save(path, quality=85)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("recording", nargs="?", type=Path, help="recording folder (default: the newest)")
    parser.add_argument("--start", type=float, default=0, help="seconds from the start of the recording")
    parser.add_argument("--end", type=float, help="seconds from the start of the recording")
    parser.add_argument("--width", type=int, default=1280, help="video width (default 1280)")
    parser.add_argument("--gif-width", type=int, default=640, help="GIF width (default 640)")
    parser.add_argument("--gif-fps", type=int, default=12, help="GIF frames per second (default 12)")
    parser.add_argument("--gif-start", type=float, help="GIF start, seconds from the start of the recording")
    parser.add_argument("--gif-end", type=float, help="GIF end, seconds from the start of the recording")
    parser.add_argument("--crf", type=int, default=24, help="video quality, higher = smaller file (default 24)")
    parser.add_argument("--blur", action="append", default=[], metavar="X,Y,W,H",
                        help="blur a region, as fractions of the video (can repeat)")
    parser.add_argument("--no-captions", action="store_true", help="leave out the caption bar")
    parser.add_argument("--mp4", type=Path, default=HERE / "docs" / "demo" / "demo.mp4")
    parser.add_argument("--gif", type=Path, default=HERE / "docs" / "images" / "demo.gif")
    args = parser.parse_args()

    folder = args.recording or max(RECORDINGS.glob("*/recording.json"), default=None, key=lambda p: p.stat().st_mtime)
    if folder is None:
        sys.exit("No recordings yet. Press R in the bridge window while playing with --xbox.")
    folder = (folder if folder.is_dir() else folder.parent).resolve()  # ffmpeg's list needs absolute paths
    info = json.loads((folder / "recording.json").read_text())
    frames = sorted(info["frames"], key=lambda f: f["t"])
    if not frames:
        sys.exit("This recording has no game frames (no Remote Play video was on screen).")
    t0 = frames[0]["t"]
    end = args.end if args.end is not None else frames[-1]["t"] - t0
    frames = [f for f in frames if args.start <= f["t"] - t0 <= end]
    blur = [tuple(float(v) for v in b.split(",")) for b in args.blur]

    out_dir = folder / "rendered"
    shutil.rmtree(out_dir, ignore_errors=True)
    out_dir.mkdir()
    lines = []
    samples = []
    step = max(1, len(frames) // 12)
    for i, frame in enumerate(frames):
        image = render(frame, folder, info["events"], args.width, blur, not args.no_captions)
        name = out_dir / f"{i:06}.png"
        image.save(name)
        if i % step == 0 and len(samples) < 12:
            samples.append(image)
        duration = (frames[i + 1]["t"] if i + 1 < len(frames) else frame["t"] + 1 / 30) - frame["t"]
        lines += [f"file '{name}'", f"duration {max(duration, 0.001):.4f}"]
    lines.append(f"file '{out_dir / f'{len(frames) - 1:06}.png'}'")  # concat needs the last file twice
    (out_dir / "list.txt").write_text("\n".join(lines) + "\n")
    contact_sheet(samples, folder / "contact-sheet.jpg")

    args.mp4.parent.mkdir(parents=True, exist_ok=True)
    args.gif.parent.mkdir(parents=True, exist_ok=True)
    run = lambda *cmd: subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *cmd], check=True)
    run("-f", "concat", "-safe", "0", "-i", str(out_dir / "list.txt"), "-fps_mode", "cfr", "-r", "30",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", str(args.crf), "-preset", "slow", "-movflags", "+faststart",
        str(args.mp4))
    gif_range = []
    if args.gif_start is not None:
        gif_range += ["-ss", f"{args.gif_start - args.start:.2f}"]
    if args.gif_end is not None:
        gif_range += ["-to", f"{args.gif_end - args.start:.2f}"]
    run(*gif_range, "-i", str(args.mp4), "-vf",
        f"fps={args.gif_fps},scale={args.gif_width}:-1:flags=lanczos,split[a][b];"
        "[a]palettegen=max_colors=128:stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=4",
        str(args.gif))

    seconds = frames[-1]["t"] - frames[0]["t"]
    for path in (args.mp4, args.gif):
        shown = path.relative_to(HERE) if path.is_relative_to(HERE) else path
        print(f"{shown}  {path.stat().st_size / 1e6:.1f} MB")
    print(f"{len(frames)} frames, {seconds:.1f} s. Check {folder / 'contact-sheet.jpg'} for names before publishing.")


if __name__ == "__main__":
    main()
