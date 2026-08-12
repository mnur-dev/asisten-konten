"""Move sounds, loaded from the recorded clips in assets/sounds."""
import functools
import subprocess
import wave
from pathlib import Path

import numpy as np

RATE = 48000

ASSETS_DIR = Path(__file__).resolve().parent.parent / "assets" / "sounds"

# event kind -> source clip
CLIPS = {
    "move": "move-self.mp3",
    "capture": "capture.mp3",
    "castle": "castle.mp3",
    "check": "move-check.mp3",
}


@functools.lru_cache(maxsize=None)
def _load(name, rate=RATE):
    """Decode a clip to mono float64 samples in [-1, 1] at `rate`."""
    path = ASSETS_DIR / name
    command = ["ffmpeg", "-v", "error", "-i", str(path), "-ac", "1", "-ar", str(rate), "-f", "s16le", "-"]
    raw = subprocess.run(command, capture_output=True, check=True).stdout
    return np.frombuffer(raw, "<i2").astype(np.float64) / 32768.0


def voices():
    """One sound per event kind, decoded from the recorded clips."""
    return {kind: _load(clip) for kind, clip in CLIPS.items()}


def classify(san: str) -> str:
    if san.startswith("O-O"):
        return "castle"
    if san.endswith("#") or san.endswith("+"):
        return "check"
    if "x" in san:
        return "capture"
    return "move"


def build_track(events, duration, path, rate=RATE, volume=1.0):
    """events: [(seconds, kind)] -> a mono 16-bit WAV of that length."""
    bank = voices()
    track = np.zeros(int(rate * duration) + rate, np.float64)
    for at, kind in events:
        sound = bank.get(kind, bank["move"])
        start = int(at * rate)
        end = min(start + len(sound), len(track))
        if start < len(track):
            track[start:end] += sound[:end - start]
    peak = np.abs(track).max()
    if peak > 1.0:
        track /= peak
    samples = np.clip(track * volume * 0.9, -1.0, 1.0)
    data = (samples * 32767).astype("<i2")
    path = Path(path)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(data.tobytes())
    return path


def events_from_plan(timeline, plan):
    """Turn the render plan into (time, kind) pairs at each board change."""
    events, clock = [], 0.0
    for ply, seconds in plan:
        if ply > 0:
            events.append((clock, classify(timeline["moves"][ply - 1]["san"])))
        clock += seconds
    return events


def mux(video, audio, output):
    """Attach the click track to a rendered board video."""
    command = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(video), "-i", str(audio),
               "-c:v", "copy", "-c:a", "aac", "-b:a", "128k", "-shortest", str(output)]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(f"Muxing audio failed: {result.stderr.strip()[-600:]}")
    return Path(output)
