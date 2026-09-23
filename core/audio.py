"""Move sounds, loaded from the recorded clips in assets/sounds."""
import functools
import json
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


def _write_wav(track, path, rate, volume):
    """Normalise a mono float track and write it out as 16-bit PCM."""
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
    return _write_wav(track, path, rate, volume)


def pop_track(times, duration, path, rate=RATE, volume=1.0):
    """A mono WAV, silent except for the caption pop at each of `times` -- seconds
    in the short's own timeline, the same clock the captions are timed against.
    Returns None when the pop clip isn't in assets/sounds, so captions simply land
    without a sound rather than failing the render."""
    clip = find_pop()
    if not clip or not times:
        return None
    sound = _load(clip, rate)
    track = np.zeros(int(rate * duration) + rate, np.float64)
    for at in times:
        start = int(max(0.0, at) * rate)
        if start >= len(track):
            continue
        end = min(start + len(sound), len(track))
        track[start:end] += sound[:end - start]
    return _write_wav(track, path, rate, volume)


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


# The track a short starts out with, so a new project needs no music picking at all.
DEFAULT_MUSIC = "Tame Impala.MP3"

# Played once at the moment each caption appears in the short.
CAPTION_POP = "pop.mp3"


def find_pop():
    """The caption pop clip as it is spelled on disk, or None when it isn't there.
    Matched case-insensitively for the same reason default_music() is: the file
    arrives however the user's downloader named it, .mp3 or .MP3."""
    if not ASSETS_DIR.is_dir():
        return None
    for entry in sorted(ASSETS_DIR.iterdir()):
        if entry.is_file() and entry.name.lower() == CAPTION_POP.lower():
            return entry.name
    return None


def available_music():
    """Audio files dropped into assets/sounds that aren't one of the move-click
    clips or the caption pop -- candidates for a short clip's background music."""
    if not ASSETS_DIR.is_dir():
        return []
    skip = set(CLIPS.values()) | {(find_pop() or "")}
    return sorted(f.name for f in ASSETS_DIR.iterdir()
                  if f.is_file() and f.name not in skip
                  and f.suffix.lower() in (".mp3", ".wav", ".m4a", ".ogg", ".flac"))


def default_music():
    """DEFAULT_MUSIC as it is actually spelled on disk, or None when that file is
    not there -- storing a name nothing matches would fail at render time, and the
    extension's case is not guaranteed to match what's typed above."""
    for name in available_music():
        if name.lower() == DEFAULT_MUSIC.lower():
            return name
    return None


def probe_duration(path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
        capture_output=True, text=True, check=True).stdout
    return float(json.loads(out)["format"]["duration"])


def add_track(video, track_path, output, volume=1.0):
    """Mix a WAV that already sits on the video's own timeline into its audio,
    leaving the video stream untouched. Used for the caption pops, which are
    positioned in the short's timeline rather than tied to any input stream.

    Same `normalize=0` plus limiter as add_music: amix otherwise halves every input
    it mixes, which would quietly drop the existing move clicks by half the moment a
    single pop was added."""
    from core.video import has_audio
    if has_audio(video):
        filter_complex = (f"[1:a]volume={volume}[pops];"
                          f"[0:a][pops]amix=inputs=2:duration=first:dropout_transition=0:"
                          f"normalize=0[mixed];[mixed]alimiter=limit=0.95[a]")
    else:
        filter_complex = f"[1:a]volume={volume}[a]"
    command = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(video), "-i", str(track_path),
               "-filter_complex", filter_complex, "-map", "0:v", "-map", "[a]",
               "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", str(output)]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(f"Adding sound effects failed: {result.stderr.strip()[-600:]}")
    return Path(output)


def add_music(video, music_path, output, offset=0.0, volume=0.8, fade=0.6):
    """Mix background music into `video`, trimmed to start `offset` seconds into
    the track and cut to the video's own length, with a short fade in/out so it
    doesn't start or stop abruptly. Mixed with the video's existing audio (move
    clicks) if it has one, otherwise used on its own.

    `offset` lets the user drag past an intro to line up a song's drop/hook with
    the highlight rather than always starting the track from 0. `volume` is a
    fraction of the music file's own native volume -- ffmpeg's amix defaults to
    normalizing (i.e. quietly halving) every input it mixes to guard against
    clipping, which made the result noticeably quieter than `volume` alone would
    suggest; `normalize=0` here turns that off so `volume` means what it says, with
    alimiter added back only as a ceiling against actual clipping.
    """
    from core.video import has_audio, probe
    duration = probe(video)["duration"]
    fade = max(0.05, min(fade, duration / 2))
    # apad + amix duration=longest, then -t: the click track stops at the last move
    # while the video holds the closing position for a second or two, so mixing to the
    # FIRST input's length cut the music short of the end (measured: 39.6s of music
    # under a 42.8s short). The output is still cut to the video by -t below.
    music_filter = (f"afade=t=in:st=0:d={fade:.3f},"
                    f"afade=t=out:st={max(0.0, duration - fade):.3f}:d={fade:.3f},"
                    f"volume={volume},apad")
    if has_audio(video):
        filter_complex = (f"[1:a]{music_filter}[music];"
                          f"[0:a][music]amix=inputs=2:duration=longest:dropout_transition=0:"
                          f"normalize=0[mixed];[mixed]alimiter=limit=0.95[a]")
    else:
        filter_complex = f"[1:a]{music_filter}[a]"
    command = ["ffmpeg", "-y", "-loglevel", "error",
               "-i", str(video), "-ss", f"{max(0.0, offset):.3f}", "-i", str(music_path),
               "-t", f"{duration:.3f}", "-filter_complex", filter_complex,
               "-map", "0:v", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
               str(output)]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(f"Adding music failed: {result.stderr.strip()[-600:]}")
    return Path(output)
