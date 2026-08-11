import json, subprocess, tempfile
from pathlib import Path
from shared.timeline import parse_pgn

PROMPT = '''Analyze ONLY physical chessboard in this contact sheet. Frame labels are absolute source-video seconds at 0.25-second intervals. Ignore digital-board overlay. Return ONLY valid JSON array, no markdown: [{"timestamp":8.75,"confidence":0.9}]. Timestamp means moved piece has been released and physical position first becomes stable. Count each physical move once. Exclude handshake, clock press, repeated stable frames, and non-move gestures. PGN move order starts: {moves}. This sheet covers {start:.2f}–{end:.2f}s.'''


def parse_json(text):
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end < start: raise RuntimeError(f"Vision model returned no JSON array: {text[-500:]}")
    return json.loads(text[start:end + 1])


def contact_sheet(video, output, start, seconds=10):
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", str(start), "-i", str(video), "-t", str(seconds), "-vf", f"fps=4,scale=320:180,drawtext=text='%{{pts\\:hms}}':x=4:y=4:fontsize=16:fontcolor=white:box=1:boxcolor=black@0.8,tile=5x8", "-frames:v", "1", str(output)], check=True)


def analyze_sheet(sheet, prompt, hermes="hermes"):
    result = subprocess.run([hermes, "-p", "asisten-konten", "chat", "-Q", "--source", "tool", "-t", "vision", "--image", str(sheet), "-q", prompt], capture_output=True, text=True, check=True)
    return parse_json(result.stdout)


def detect_with_hermes(video, pgn, output, hermes="hermes", segment_seconds=10):
    video, pgn, output = Path(video), Path(pgn), Path(output)
    timeline = parse_pgn(pgn.read_text(encoding="utf-8-sig")); moves = " ".join(item["san"] for item in timeline["moves"])
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(video)], capture_output=True, text=True, check=True)
    duration = float(probe.stdout); found = []
    with tempfile.TemporaryDirectory() as directory:
        for start in range(0, int(duration + segment_seconds - 1), segment_seconds):
            sheet = Path(directory) / f"sheet-{start:05d}.jpg"; contact_sheet(video, sheet, start, min(segment_seconds, duration-start))
            prompt = PROMPT.format(moves=moves[len(found):len(found)+20], start=start, end=min(duration, start+segment_seconds))
            for item in analyze_sheet(sheet, prompt, hermes):
                timestamp = float(item["timestamp"])
                if start <= timestamp <= min(duration, start+segment_seconds) and (not found or timestamp-found[-1]["timestamp"] >= .5):
                    found.append({"ply": len(found)+1, "timestamp": timestamp, "confidence": float(item.get("confidence", .5))})
    if len(found) != len(timeline["moves"]): raise RuntimeError(f"AI detected {len(found)} moves for {len(timeline['moves'])} PGN plies")
    output.write_text(json.dumps(found, indent=2)+"\n", encoding="utf-8"); return found
