import json, subprocess, tempfile
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont
from shared.timeline import parse_pgn

PROMPT = '''Inspect every labeled frame of this contact sheet in chronological order. The image is cropped around the PHYSICAL chessboard; ignore any digital-board overlay still visible. Detect every real move where a hand relocates a physical piece. Return ONLY a valid JSON array, no markdown: [{{"timestamp":8.75,"confidence":0.9}}]. Timestamp is the first labeled frame where the moved piece has been released on its destination square; do not wait for clock press or complete stillness. Count fast consecutive moves separately. Exclude handshake, clock press, repeated stable frames, camera changes, and gestures without a piece relocation. Do not return objects without timestamp. Relevant PGN sequence: {moves}. Sheet interval: {start:.2f}–{end:.2f}s.'''


def format_prompt(moves, start, end):
    return PROMPT.format(moves=moves, start=start, end=end)


def parse_json(text):
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end < start: raise RuntimeError(f"Vision model returned no JSON array: {text[-500:]}")
    return json.loads(text[start:end + 1])


def contact_sheet(video, output, start, seconds=10):
    output = Path(output)
    with tempfile.TemporaryDirectory() as directory:
        pattern = str(Path(directory) / "%04d.jpg")
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", str(start), "-i", str(video), "-t", str(seconds), "-vf", "fps=4,crop=640:360:320:340,scale=320:180", pattern], check=True)
        frames = sorted(Path(directory).glob("*.jpg"))
        if not frames: raise RuntimeError("FFmpeg created no contact-sheet frames")
        sheet = Image.new("RGB", (1600, 1440), "black")
        font = ImageFont.load_default()
        for index, frame in enumerate(frames[:40]):
            image = Image.open(frame).convert("RGB"); draw = ImageDraw.Draw(image)
            label = f"{start + index / 4:.2f}s"; draw.rectangle((0, 0, 76, 18), fill="black"); draw.text((3, 3), label, fill="white", font=font)
            sheet.paste(image, ((index % 5) * 320, (index // 5) * 180))
        sheet.save(output, "JPEG", quality=90)


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
            prompt = format_prompt(moves[len(found):len(found)+20], start, min(duration, start+segment_seconds))
            for item in analyze_sheet(sheet, prompt, hermes):
                timestamp = float(item["timestamp"])
                if start <= timestamp <= min(duration, start+segment_seconds) and (not found or timestamp-found[-1]["timestamp"] >= .5):
                    found.append({"ply": len(found)+1, "timestamp": timestamp, "confidence": float(item.get("confidence", .5))})
    if len(found) != len(timeline["moves"]): raise RuntimeError(f"AI detected {len(found)} moves for {len(timeline['moves'])} PGN plies")
    output.write_text(json.dumps(found, indent=2)+"\n", encoding="utf-8"); return found
