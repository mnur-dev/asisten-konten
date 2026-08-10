import json, subprocess
from pathlib import Path


def detect_timestamps(video, ply_count, output, sample_fps=4, roi=(0.40, 0.02, 0.22, 0.43)):
    """Detect move times from fixed digital-board overlay using clustered frame differences."""
    video, output = Path(video), Path(output)
    probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height", "-of", "json", str(video)], capture_output=True, text=True, check=True)
    stream = json.loads(probe.stdout)["streams"][0]; width, height = stream["width"], stream["height"]
    x, y, w, h = roi
    crop = (round(width*x), round(height*y), max(16, round(width*w)), max(16, round(height*h)))
    command = ["ffmpeg", "-v", "error", "-i", str(video), "-vf", f"fps={sample_fps},crop={crop[2]}:{crop[3]}:{crop[0]}:{crop[1]},scale=64:64,format=gray", "-f", "rawvideo", "-"]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    frame_size = 64 * 64; previous = process.stdout.read(frame_size); scores = []
    index = 1
    while frame := process.stdout.read(frame_size):
        if len(frame) != frame_size: break
        scores.append((sum(abs(a-b) for a, b in zip(frame, previous)) / frame_size, index / sample_fps))
        previous = frame; index += 1
    error = process.stderr.read().decode(errors="replace"); code = process.wait()
    if code: raise RuntimeError(f"FFmpeg analysis failed: {error.strip()}")
    candidates = []
    for i in range(1, len(scores)-1):
        score, timestamp = scores[i]
        if score >= scores[i-1][0] and score > scores[i+1][0]: candidates.append((score, timestamp))
    selected = []
    for score, timestamp in sorted(candidates, reverse=True):
        if all(abs(timestamp-existing[1]) >= 0.75 for existing in selected):
            selected.append((score, timestamp))
            if len(selected) == ply_count: break
    if len(selected) != ply_count: raise RuntimeError(f"Found only {len(selected)} move candidates for {ply_count} plies")
    selected.sort(key=lambda item: item[1]); median = sorted(score for score, _ in selected)[len(selected)//2] or 1
    data = [{"ply": ply, "timestamp": round(timestamp, 3), "confidence": round(min(1.0, score/median), 3)} for ply, (score, timestamp) in enumerate(selected, 1)]
    output.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return data
