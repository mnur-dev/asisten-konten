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
CALIBRATION_WINDOWS = 4   # broadcasts move or resize the overlay partway through
MIN_MATCH_RATE = 0.15     # below this the overlay is not showing this game
# Refined rectangles within this many pixels are the same layout. Generous, because
# two windows refining the same overlay disagree by a few px; a broadcast that really
# does move its overlay moves it much further than this (measured: 33 and 56 px).
SAME_RECT = 24


def _same_layout(layout, other):
    """Same rectangle and same way up. A board drawn from the other side is not a
    near-miss of this one, however closely the two rectangles agree."""
    (rect, flipped), (other_rect, other_flipped) = layout, other
    return (flipped == other_flipped
            and max(abs(a - b) for a, b in zip(rect, other_rect)) <= SAME_RECT)


def _shared_score(frames, layout, references):
    """(capped cost sum, -exact matches) for a layout over one shared set of frames.

    Per-window match counts cannot be compared with each other. They are counted on
    different frames, and how many of a window's frames are informative depends on
    what the broadcast was showing then, not on how good the rectangle is: a window
    that catches the pre-game build-up scores low however well it was refined. Two
    windows that landed on the same layout have to be settled on the same evidence,
    so the winner is picked here, on frames spanning the whole video.
    """
    rect, flipped = layout
    window = overlay.scale_window(frames, rect)
    wanted = overlay.oriented(references, flipped)
    read = overlay.signatures(window, overlay.calibrate_colour(window, wanted))
    cost = (read[:, None] != wanted[None]).sum((2, 3)).min(1)
    return float(np.minimum(cost, overlay.COST_CAP).sum()), -int((cost == 0).sum())


def calibrate(video, references, duration, info):
    """Find every overlay layout the broadcast uses, and verify each against the PGN.

    locate_geometric medians the frames it is handed, so one global call only ever
    finds the layout that dominates the timeline. Broadcasts do move the overlay
    mid-game -- measured on real videos: one shifted 674 -> 618 px, another went
    (602, 8, 528) -> (569, 9, 560) -- and under a single fixed rectangle every frame
    from the other layout reads as noise, silently losing that whole stretch of the
    game (29 opening plies in the 674 -> 618 case). Looking window by window surfaces
    each layout as its own candidate, and each is then proven against the PGN over the
    window it came from. Returns (rects, best_rate), strongest proof first.

    Every window is refined, even when it starts from the same rectangle as an earlier
    one: how accurately a window can be refined depends on how much of the game is
    actually on screen during it, and a window that catches the pre-game build-up
    refines poorly. Windows that land on the same layout are merged afterwards, and
    the survivor is chosen by _shared_score on every window's frames at once -- their
    own match counts are not comparable, and picking by them hands the group to
    whichever window happened to be richest in play rather than to the best rectangle.
    Measured on MVL-Carlsen, where four windows agreed on one layout to within 9px and
    the count picked the one rectangle of the four that read the video worst.
    """
    shape = (info["width"], info["height"])
    edges = np.linspace(0.05, 0.95, CALIBRATION_WINDOWS + 1)
    per_window = max(2, CALIBRATION_FRAMES // CALIBRATION_WINDOWS)
    candidates, sampled, best_rate = [], [], 0.0
    for low, high in zip(edges, edges[1:]):
        start, length = duration * low, duration * (high - low)
        frames = np.concatenate(
            [sample_gray(video, 1, shape, start=float(s), count=1)
             for s in np.linspace(start, start + length, per_window)])
        sampled.append(frames)
        score, rect = overlay.locate_geometric(frames)
        log.info("Overlay candidate at %s for %.0f-%.0fs (checkerboard %.2f/8)",
                 rect, start, start + length, score)
        rect, flipped, exact, tested = overlay.refine_against_pgn(
            video, rect, references, frame_size=shape, window=(start, length))
        rate = exact / max(tested, 1)
        best_rate = max(best_rate, rate)
        log.info("  refined to %s%s | exact position matches %d/%d (%.0f%%)",
                 rect, " (drawn from Black's side)" if flipped else "",
                 exact, tested, rate * 100)
        if rate < MIN_MATCH_RATE:
            continue
        candidates.append((exact, (rect, flipped)))
    if not candidates:
        return [], best_rate

    shared = np.concatenate(sampled)
    proven = []
    for exact, layout in candidates:
        score = _shared_score(shared, layout, references)
        for index, (other_exact, other_score, other_layout) in enumerate(proven):
            if _same_layout(layout, other_layout):
                if score < other_score:
                    log.info("  %s replaces %s for this layout (cost %.0f vs %.0f "
                             "over %d shared frames)", layout[0], other_layout[0],
                             score[0], other_score[0], len(shared))
                    proven[index] = (max(exact, other_exact), score, layout)
                break
        else:
            proven.append((exact, score, layout))
    proven.sort(key=lambda item: -item[0])
    return [layout for _, _, layout in proven], best_rate


def detect(video, pgn_path, output=None, fps=4.0):
    video, pgn_path = Path(video), Path(pgn_path)
    timeline = parse_pgn(pgn_path.read_text(encoding="utf-8-sig"))
    references = pgn_signatures(timeline)
    info = probe(video)
    log.info("%s | %dx%d | %.1fs | %d plies",
             video.name, info["width"], info["height"], info["duration"], len(timeline["moves"]))

    layouts, rate = calibrate(video, references, info["duration"], info)
    if not layouts:
        raise RuntimeError(
            f"Overlay board does not track this game (only {rate*100:.0f}% of sampled frames "
            "match any position). It is probably showing a different board.")

    # Read the video once per proven layout and keep the better reading of each frame.
    # A layout that is wrong for a given frame crops off the board entirely, so it
    # disagrees with every real position; the layout that does frame the board there
    # reads it exactly. Taking the cheaper of the two per (frame, ply) therefore picks
    # the truth without having to know where the broadcast switched.
    cost = None
    for rect, flipped in layouts:
        frames = sample_gray(video, fps, overlay.GRID, crop=rect)
        wanted = overlay.oriented(references, flipped)
        calibration = overlay.calibrate_colour(frames, wanted)
        log.info("Sampled %d frames at %g fps under %s%s | colour %s",
                 len(frames), fps, rect, " flipped" if flipped else "",
                 "level %.0f, cuts %.2f/%.2f" % calibration if calibration
                 else "fixed thresholds")
        layer = align.match_cost(overlay.signatures(frames, calibration), wanted)
        if cost is None:
            cost = layer
        else:
            shared = min(len(cost), len(layer))
            cost = np.minimum(cost[:shared], layer[:shared])
    path = align.solve(cost)
    points = align.waypoints(path, cost, fps)

    observed = [p for p in points if p["observed"]]
    log.info("Plies directly observed: %d/%d", len(observed), len(points))
    result = {
        "video": str(video),
        "pgn": str(pgn_path),
        "source": "overlay",
        "overlay_rect": list(layouts[0][0]),   # strongest layout; the paste-box default
        "overlay_rects": [list(rect) for rect, _ in layouts],
        "overlay_flipped": bool(layouts[0][1]),
        "overlay_flips": [bool(flipped) for _, flipped in layouts],
        "fps": fps,
        "duration": info["duration"],
        "plies_total": len(timeline["moves"]),
        "plies_observed": len(observed),
        "waypoints": points,
    }
    if output:
        Path(output).write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result, timeline
