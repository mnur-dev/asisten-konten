"""FFmpeg helpers: probing and raw grayscale sampling."""
import json
import subprocess
import sys
from pathlib import Path

import numpy as np


def download(url, output) -> Path:
    """Fetch a video with yt-dlp, merged into a single mp4 at `output`."""
    output = Path(output)
    command = [sys.executable, "-m", "yt_dlp", "-f",
               "bv*[height<=1080]+ba/b[height<=1080]/best",
               "--merge-output-format", "mp4", "--no-playlist", "-o", str(output), url]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(f"Video download failed: {result.stderr.strip()[-800:]}")
    if not output.is_file():
        raise RuntimeError("yt-dlp reported success but wrote no file")
    return output


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


def sample_gray(path, fps, size, crop=None, start=None, count=None):
    """Decode to raw gray frames -> (n, h, w) uint8.

    `size` is an int for a square output or (width, height). `crop` is (x, y, side).
    """
    width, height = (size, size) if isinstance(size, int) else size
    filters = [f"fps={fps}"]
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
