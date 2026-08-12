"""Locate every ply of a PGN inside a broadcast video, using the overlay board."""
import json
import logging
from pathlib import Path

import numpy as np

from core import align, overlay
from core.pgn import parse_pgn, signatures as pgn_signatures
from core.video import probe, sample_gray

log = logging.getLogger(__name__)

CALIBRATION_FRAMES = 24
MIN_MATCH_RATE = 0.15     # below this the overlay is not showing this game


def calibrate(video, references, duration, info):
    """Find the overlay board, then verify it actually tracks this PGN."""
    starts = np.linspace(duration * 0.1, duration * 0.9, CALIBRATION_FRAMES)
    shape = (info["width"], info["height"])
    frames = np.concatenate(
        [sample_gray(video, 1, shape, start=float(s), count=1) for s in starts])
    score, rect = overlay.locate_geometric(frames)
    log.info("Overlay candidate at %s (checkerboard %.2f/8)", rect, score)
    rect, exact, tested = overlay.refine_against_pgn(video, rect, references, frame_size=shape)
    rate = exact / max(tested, 1)
    log.info("Refined to %s | exact occupancy matches %d/%d (%.0f%%)", rect, exact, tested, rate * 100)
    return rect, rate


def detect(video, pgn_path, output=None, fps=4.0):
    video, pgn_path = Path(video), Path(pgn_path)
    timeline = parse_pgn(pgn_path.read_text(encoding="utf-8-sig"))
    references = pgn_signatures(timeline)
    info = probe(video)
    log.info("%s | %dx%d | %.1fs | %d plies",
             video.name, info["width"], info["height"], info["duration"], len(timeline["moves"]))

    rect, rate = calibrate(video, references, info["duration"], info)
    if rate < MIN_MATCH_RATE:
        raise RuntimeError(
            f"Overlay board does not track this game (only {rate*100:.0f}% of sampled frames "
            "match any position). It is probably showing a different board.")

    frames = sample_gray(video, fps, overlay.GRID, crop=rect)
    log.info("Sampled %d frames at %g fps", len(frames), fps)
    signatures = overlay.signatures(frames)
    cost = align.match_cost(signatures, references)
    path = align.solve(cost)
    points = align.waypoints(path, cost, fps)

    observed = [p for p in points if p["observed"]]
    log.info("Plies directly observed: %d/%d", len(observed), len(points))
    result = {
        "video": str(video),
        "pgn": str(pgn_path),
        "source": "overlay",
        "overlay_rect": list(rect),
        "fps": fps,
        "duration": info["duration"],
        "plies_total": len(timeline["moves"]),
        "plies_observed": len(observed),
        "waypoints": points,
    }
    if output:
        Path(output).write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result, timeline
