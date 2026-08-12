"""Move sounds, synthesised rather than shipped.

A wooden piece landing on a board is a struck-object sound: a handful of decaying
resonant modes plus a short noise transient from the contact. Modelling it that
way keeps the project free of binary assets and licences, and lets capture and
check sound different without needing more files.
"""
import struct
import subprocess
import wave
from pathlib import Path

import numpy as np

RATE = 48000

# (frequency Hz, amplitude, decay seconds) — the modes of a small wooden block
WOOD = [(196, 1.00, 0.055), (523, 0.55, 0.040), (1180, 0.30, 0.026), (2640, 0.16, 0.014)]


def _click(gain=1.0, brightness=1.0, length=0.13, seed=0):
    n = int(RATE * length)
    t = np.arange(n) / RATE
    signal = np.zeros(n, np.float64)
    for frequency, amplitude, decay in WOOD:
        signal += amplitude * np.sin(2 * np.pi * frequency * brightness * t) * np.exp(-t / decay)
    # contact transient: a very short burst of noise, high-passed by differencing
    rng = np.random.default_rng(seed)
    burst = rng.standard_normal(n) * np.exp(-t / 0.0035)
    signal += 0.55 * np.diff(burst, prepend=0.0)
    signal *= 1 - np.exp(-t / 0.0008)          # remove the click at sample zero
    peak = np.abs(signal).max()
    return (signal / peak * gain) if peak else signal


def voices():
    """One sound per event kind, all derived from the same wooden model."""
    return {
        "move": _click(0.55, 1.00, seed=1),
        "capture": _click(0.85, 1.18, length=0.16, seed=2),
        "castle": _click(0.62, 0.92, length=0.17, seed=3),
        "check": _click(0.80, 1.30, length=0.18, seed=4),
    }


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
