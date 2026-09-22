"""FFmpeg helpers: probing and raw grayscale sampling."""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import numpy as np

_PROGRESS_RE = re.compile(r"\[download]\s+([\d.]+)% of\s+\S+.*?(?:ETA\s+(\S+))?\s*$")


def _run_yt_dlp(command, on_progress):
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, bufsize=1)
    tail = []
    for line in process.stdout:
        line = line.rstrip("\n")
        tail.append(line)
        del tail[:-40]
        if on_progress and (match := _PROGRESS_RE.search(line)):
            on_progress(float(match.group(1)), match.group(2))
    process.wait()
    return process.returncode, chr(10).join(tail)[-800:]


def cookie_file() -> Path | None:
    """A Netscape cookies.txt exported from a logged-in browser, if one is configured.

    YT_COOKIES names it; a cookies.txt at the repo root is the fallback (gitignored --
    it is a live login). This is what makes downloads work on the VPS: YouTube answers
    a datacenter IP with "Sign in to confirm you're not a bot" before listing a single
    format, and there is no browser profile there for --cookies-from-browser. yt-dlp
    writes rotated cookies back into the file, so it has to stay writable; that keeps
    the session alive longer than the exported copy would on its own.
    """
    configured = os.environ.get("YT_COOKIES", "").strip()
    path = Path(configured) if configured else Path(__file__).resolve().parents[1] / "cookies.txt"
    return path if path.is_file() else None


def download(url, output, on_progress=None) -> Path:
    """Fetch a video with yt-dlp, merged into a single mp4 at `output`.

    `on_progress`, if given, is called as on_progress(percent, eta) while the
    download runs -- percent 0-100, eta a "MM:SS" string or None near the end.

    Plain requests get throttled mid-download (HTTP 403) on YouTube's higher-quality
    adaptive formats once anti-bot heuristics kick in -- reproduced on a video that
    403'd every time at ~8% with the plain command, downloaded clean at full 1080p
    once authenticated. Browser cookies plus yt-dlp's remote JS-challenge solver keep
    the session looking legitimate; that's tried first, falling back to the plain
    command for machines without a usable Firefox profile.
    """
    output = Path(output)
    for leftover in output.parent.glob(output.stem + ".*"):
        leftover.unlink(missing_ok=True)
    common = ["-f", "bv*[height<=1080]+ba/b[height<=1080]/best",
              "--merge-output-format", "mp4", "--no-playlist", "--newline", "--progress",
              "-o", str(output), url]
    variants = [
        ["--cookies-from-browser", "firefox", "--remote-components", "ejs:github"],
        [],
    ]
    if cookies := cookie_file():
        variants.insert(0, ["--cookies", str(cookies), "--remote-components", "ejs:github"])
    returncode, detail, cookie_detail = 1, "no attempt ran", None
    for extra in variants:
        returncode, detail = _run_yt_dlp(
            [sys.executable, "-m", "yt_dlp", *extra, *common], on_progress)
        if not returncode and output.is_file():
            return output
        if extra[:1] == ["--cookies"]:
            cookie_detail = detail
        for leftover in output.parent.glob(output.stem + ".*"):
            leftover.unlink(missing_ok=True)
    # With a cookie file configured, its failure is the one worth reading: wherever it
    # was needed, the attempts after it fail with the bot check and hide an expired login.
    raise RuntimeError(f"Video download failed: {cookie_detail or detail}")


def probe(path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height,r_frame_rate",
         "-show_entries", "format=duration", "-of", "json", str(path)],
        capture_output=True, text=True, check=True).stdout
    data = json.loads(out)
    stream = data["streams"][0]
    return {
        "width": int(stream["width"]),
        "height": int(stream["height"]),
        "duration": float(data["format"]["duration"]),
    }


def source_info(url) -> dict:
    """Title and uploader of the source video, without downloading it -- the title
    suggester works from it. Same cookie-first variants as download(), for the same
    bot-check reason; {} when every attempt fails, since a missing title only
    weakens the suggestions rather than blocking them."""
    variants = [[]]
    if cookies := cookie_file():
        variants.insert(0, ["--cookies", str(cookies), "--remote-components", "ejs:github"])
    for extra in variants:
        out = subprocess.run([sys.executable, "-m", "yt_dlp", *extra, "--skip-download",
                              "--no-playlist", "--dump-single-json", url],
                             capture_output=True, text=True, timeout=120)
        if out.returncode == 0 and out.stdout.strip():
            data = json.loads(out.stdout)
            return {"title": data.get("title") or "", "channel": data.get("uploader") or data.get("channel") or ""}
    return {}


PREVIEW_HEIGHT = 540
PREVIEW_GOP = 30            # one keyframe a second at 30 fps


def make_preview(source, output) -> Path:
    """A light copy of the broadcast for the UI's scrubber: 540p, silent, and a
    keyframe every second. Broadcast files come with ~5 s between keyframes at
    1080p, so every slider step made the browser decode up to 150 full-HD frames --
    quick on a phone's hardware decoder, heavy on a desktop decoding in software.
    Measured on a 9-minute broadcast: 118 MB -> 34 MB, ~150 s on the VPS at nice 10.
    Written to a temp name and renamed, so a half-made file is never served."""
    output = Path(output)
    partial = output.with_name(output.stem + ".partial.mp4")
    command = ["nice", "-n", "10", "ffmpeg", "-v", "error", "-y", "-i", str(source),
               "-vf", f"scale=-2:{PREVIEW_HEIGHT}", "-c:v", "libx264", "-preset", "veryfast",
               "-crf", "28", "-g", str(PREVIEW_GOP), "-keyint_min", str(PREVIEW_GOP),
               "-sc_threshold", "0", "-pix_fmt", "yuv420p", "-an", "-movflags", "+faststart",
               str(partial)]
    if os.name == "nt":                                   # no `nice` on Windows
        command = command[3:]
    subprocess.run(command, check=True, capture_output=True)
    partial.replace(output)
    return output


def has_audio(path) -> bool:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True).stdout
    return bool(out.strip())


def sample_gray(path, fps, size, crop=None, start=None, count=None):
    """Decode to raw gray frames -> (n, h, w) uint8.

    `size` is an int for a square output or (width, height). `crop` is (x, y, side).

    Gray comes before the crop, not after it. FFmpeg's crop filter snaps x and y down
    onto the chroma grid, so on yuv420p an odd offset is silently read one pixel away:
    crop=519:519:715:23 returns byte-identical output to :714:22. The search that picks
    the overlay rectangle works at single-pixel resolution and reads its candidates
    through PIL, which honours them -- so a rectangle proved at an odd offset was then
    read two pixels off here, and nothing reported a disagreement. Measured on
    MVL-Carlsen, where the winning rectangle matched a position exactly in 259 of 494
    frames as it was proved and 2 as it was read, leaving 22 of 109 plies observed.
    Converting to gray first drops the subsampled planes, and crop then honours the
    coordinates it was given.

    The `scale=iw:ih` in front of it is not a no-op, and removing it doubles the cost
    of every call. FFmpeg negotiates formats backwards along the chain, and filters
    that do not care about the format -- `fps` and `crop` among them -- pass the demand
    further up. `scale` is the one that absorbs it, so with nothing between `fps` and
    the gray conversion the demand reaches the decoder itself and every decoded frame
    is converted, not just the handful `fps` keeps. Measured on one refine window: 18
    frames out either way, 9.4s with the old chain, 17.8s without this pin, 8.9s with
    it -- and byte-identical output.
    """
    width, height = (size, size) if isinstance(size, int) else size
    filters = [f"fps={fps}", "scale=iw:ih", "format=gray"]
    if crop:
        x, y, side = crop
        filters.append(f"crop={side}:{side}:{x}:{y}")
    filters.append(f"scale={width}:{height}")
    filters.append("format=gray")
    command = ["ffmpeg", "-v", "error"]
    if start is not None:
        command += ["-ss", str(start)]
    command += ["-i", str(path)]
    if count is not None:
        command += ["-frames:v", str(count)]
    command += ["-vf", ",".join(filters), "-f", "rawvideo", "-"]
    raw = subprocess.run(command, capture_output=True, check=True).stdout
    frame = width * height
    usable = len(raw) // frame * frame
    if not usable:
        raise RuntimeError(f"FFmpeg produced no frames for {path}")
    return np.frombuffer(raw[:usable], np.uint8).reshape(-1, height, width)
