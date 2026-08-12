"""Re-time plies against the wooden board instead of the broadcast graphic.

The overlay is authoritative about *which* ply is on screen -- it matches the PGN
exactly -- but not about *when* the move was played. The graphics operator runs
about a second late, and in a time scramble that stretches to several seconds.

So the overlay stays in charge of identity and provides a search window, and the
physical board decides the timing inside it. The correction is deliberately
bounded: an overlay time that is roughly right is worth more than a physical
estimate that has locked onto the wrong event.
"""
import logging
import subprocess

import numpy as np

log = logging.getLogger(__name__)

SAMPLE_FPS = 10
SAMPLE_WIDTH = 384
LOOKBACK = 6.0        # the overlay lags, so the truth is earlier -- never much later
LOOKAHEAD = 0.75
MAX_SHIFT = 5.0       # beyond this we distrust ourselves and keep the overlay time
REVIEW_SHIFT = 1.0    # under a second of disagreement is close enough; past it, ask for eyes
MIN_SEPARATION = 1.0  # below this the window holds no visible change at all


def sample_board(video, rect, fps=SAMPLE_FPS, width=SAMPLE_WIDTH):
    """Grayscale frames of the board region only. `rect` is (x, y, w, h) in source pixels."""
    x, y, w, h = rect
    height = max(16, int(round(width * h / w)))
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(video), "-vf",
         f"fps={fps},crop={w}:{h}:{x}:{y},scale={width}:{height},format=gray",
         "-f", "rawvideo", "-"], capture_output=True, check=True).stdout
    size = width * height
    usable = len(raw) // size * size
    if not usable:
        raise RuntimeError("FFmpeg produced no frames for the board region")
    return np.frombuffer(raw[:usable], np.uint8).reshape(-1, size).astype(np.float32)


def changepoint(frames, fps, low, high, guard=0.5):
    """Split [low, high] where the board stops looking 'before' and starts looking 'after'."""
    a, b = max(0, int(round(low * fps))), min(len(frames), int(round(high * fps)))
    if b - a < 6:
        return None, 0.0
    window = frames[a:b]
    edge = max(2, int(round(guard * fps)))
    if len(window) < 2 * edge + 2:
        edge = max(1, len(window) // 4)
    before = np.median(window[:edge], 0)
    after = np.median(window[-edge:], 0)
    separation = float(np.abs(after - before).mean())
    if separation < MIN_SEPARATION:
        return None, separation
    to_before = np.abs(window - before).mean(1)
    to_after = np.abs(window - after).mean(1)
    cost = (np.concatenate([[0.0], np.cumsum(to_before)])
            + np.concatenate([np.cumsum(to_after[::-1])[::-1], [0.0]]))
    return (a + int(np.argmin(cost))) / fps, separation


def refine(video, waypoints, rect, fps=SAMPLE_FPS, lookback=LOOKBACK,
           max_shift=MAX_SHIFT, frames=None):
    """Return waypoints re-timed against the physical board, with provenance on each."""
    frames = sample_board(video, rect, fps) if frames is None else frames
    duration = len(frames) / fps
    timed = [w for w in waypoints if w.get("timestamp") is not None]
    if not timed:
        raise RuntimeError("Run overlay detection before physical re-timing")

    def baseline(point):
        """Always re-time from the overlay, never from a previous physical pass."""
        return float(point.get("overlay_timestamp", point["timestamp"]))

    results, previous = [], 0.0
    for index, point in enumerate(timed):
        if point.get("edited"):                    # a human already decided this one
            results.append(dict(point))
            previous = max(previous, float(point["timestamp"]))
            continue
        overlay_time = baseline(point)
        earlier = baseline(timed[index - 1]) if index else 0.0
        low = max(previous, min(earlier, overlay_time - lookback))
        high = min(duration, overlay_time + LOOKAHEAD)
        found, separation = changepoint(frames, fps, low, high)

        updated = dict(point)
        updated["overlay_timestamp"] = overlay_time
        shift = None if found is None else found - overlay_time
        rejected = (found is None or found <= previous or abs(shift) > max_shift)
        if rejected:
            updated["source"] = "overlay"
            updated["shift"] = 0.0
            updated["needs_review"] = found is not None and abs(shift) > max_shift
            previous = max(previous, overlay_time)
        else:
            updated["timestamp"] = round(float(found), 3)
            updated["source"] = "physical"
            updated["shift"] = round(float(shift), 3)
            updated["separation"] = round(separation, 2)
            updated["needs_review"] = abs(shift) > REVIEW_SHIFT
            previous = found
        results.append(updated)

    moved = [r["shift"] for r in results if r["source"] == "physical"]
    flagged = sum(1 for r in results if r.get("needs_review"))
    if moved:
        log.info("Physical re-timing: %d/%d plies moved, median %+.2fs, "
                 "p10 %+.2fs, %d flagged for review",
                 len(moved), len(results), float(np.median(moved)),
                 float(np.percentile(moved, 10)), flagged)
    by_ply = {r["ply"]: r for r in results}
    return [by_ply.get(w["ply"], w) for w in waypoints]
