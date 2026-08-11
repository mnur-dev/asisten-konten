import json, subprocess
from pathlib import Path

# Calibrated physical 8x8 grid for 1280x720 Carlsen–Tsydypov broadcast.
BOARD_QUAD = ((524, 407), (872, 439), (807, 628), (397, 568))


def stable_end_times(scores, threshold=2.5, stable_seconds=1.0):
    if len(scores) < 2: return []
    step = scores[1][0] - scores[0][0]; gap_frames = max(1, round(stable_seconds / step))
    results = []; active = False; active_frames = quiet = 0
    for timestamp, score in scores:
        if score > threshold:
            active = True; active_frames += 1; quiet = 0
        elif active:
            quiet += 1
            if quiet >= gap_frames:
                if active_frames >= 2: results.append(round(timestamp - (gap_frames - 1) * step, 3))
                active = False; active_frames = quiet = 0
    return results


def detect_timestamps(video, ply_count, output, sample_fps=4, quad=BOARD_QUAD):
    """Detect move completion from motion ending over calibrated physical board."""
    video, output = Path(video), Path(output)
    probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height", "-of", "json", str(video)], capture_output=True, text=True, check=True)
    stream = json.loads(probe.stdout)["streams"][0]; width, height = stream["width"], stream["height"]
    if (width, height) != (1280, 720): raise RuntimeError(f"Physical-board calibration requires 1280x720 video, got {width}x{height}")
    xs, ys = [p[0] for p in quad], [p[1] for p in quad]; x, y = min(xs), min(ys); w, h = max(xs)-x, max(ys)-y
    command = ["ffmpeg", "-v", "error", "-i", str(video), "-vf", f"fps={sample_fps},crop={w}:{h}:{x}:{y},scale=128:64,format=gray", "-f", "rawvideo", "-"]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    frame_size = 128 * 64; previous = process.stdout.read(frame_size); scores = []; index = 1
    while frame := process.stdout.read(frame_size):
        if len(frame) != frame_size: break
        scores.append((index / sample_fps, sum(abs(a-b) for a, b in zip(frame, previous)) / frame_size))
        previous = frame; index += 1
    error = process.stderr.read().decode(errors="replace"); code = process.wait()
    if code: raise RuntimeError(f"FFmpeg analysis failed: {error.strip()}")
    timestamps = stable_end_times(scores)
    if len(timestamps) != ply_count:
        raise RuntimeError(f"Detected {len(timestamps)} physical-board moves for {ply_count} PGN plies; automatic result rejected")
    data = [{"ply": ply, "timestamp": timestamp, "confidence": 0.5} for ply, timestamp in enumerate(timestamps, 1)]
    output.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return data
