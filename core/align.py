"""Monotone alignment of sampled frames to PGN plies.

The ply shown on screen never goes backwards, so the frame->ply assignment is a
shortest path under that constraint. Solving it globally means a few misread
frames cannot derail the result the way a greedy threshold does.
"""
import numpy as np

CAP = 6           # costs above this carry no information (overlay off, or another board)
JUMP = 2.0        # penalty per ply skipped in one step
OBSERVED = 2      # a ply counts as directly observed at this match cost or better


def match_cost(signatures, references) -> np.ndarray:
    """(frames, plies+1) count of squares that disagree."""
    return (signatures[:, None] != references[None]).sum((2, 3)).astype(np.float32)


def solve(cost: np.ndarray) -> np.ndarray:
    """Cheapest non-decreasing ply path through the cost matrix."""
    frames, plies = cost.shape
    emission = np.minimum(cost, CAP)
    dp = emission[0].copy()
    parents = np.empty((frames, plies), np.int32)
    parents[0] = np.arange(plies)
    ladder = np.empty(plies, np.float32)      # A[p] = min_{p'<=p} previous[p'] + JUMP*(p-p')
    ladder_arg = np.empty(plies, np.int32)
    best = np.empty(plies, np.float32)
    argbest = np.empty(plies, np.int32)
    for f in range(1, frames):
        previous = dp
        ladder[0], ladder_arg[0] = previous[0], 0
        for p in range(1, plies):
            lifted = ladder[p - 1] + JUMP
            if previous[p] <= lifted:
                ladder[p], ladder_arg[p] = previous[p], p
            else:
                ladder[p], ladder_arg[p] = lifted, ladder_arg[p - 1]
        best[0], argbest[0] = previous[0], 0
        for p in range(1, plies):
            # staying put or advancing one ply is free; longer skips pay JUMP each
            if previous[p] <= ladder[p - 1]:
                best[p], argbest[p] = previous[p], p
            else:
                best[p], argbest[p] = ladder[p - 1], ladder_arg[p - 1]
        dp = emission[f] + best
        parents[f] = argbest
    path = np.empty(frames, np.int32)
    path[-1] = int(dp.argmin())
    for f in range(frames - 1, 0, -1):
        path[f - 1] = parents[f][path[f]]
    return path


def waypoints(path, cost, fps) -> list[dict]:
    """First timestamp of each ply on the path, with its match evidence.

    solve() can hold a ply from frame 0 with no real evidence for it yet: when
    nothing informative has appeared, every ply's emission is equally capped, so
    starting the path at a later ply immediately is free -- it dodges a JUMP penalty
    it would otherwise pay once real evidence for that ply does appear. That makes
    `path`'s very first frames unreliable as "when the ply began"; only a frame
    below CAP actually carries information, so the first timestamp must come from
    those, not from wherever the path happens to already be sitting.
    """
    plies = cost.shape[1]
    result = []
    for ply in range(1, plies):
        frames = np.where(path == ply)[0]
        if not len(frames):
            result.append({"ply": ply, "timestamp": None, "cost": None, "observed": False})
            continue
        raw = cost[frames, ply]
        best = int(frames[int(raw.argmin())])
        informative = frames[raw < CAP]
        if not len(informative):
            result.append({"ply": ply, "timestamp": None, "cost": None, "observed": False})
            continue
        first = int(informative[0])
        result.append({
            "ply": ply,
            "timestamp": round(first / fps, 3),
            "cost": float(raw.min()),
            "observed": bool(raw.min() <= OBSERVED),
            "support": int((raw <= OBSERVED).sum()),
            "best_frame_time": round(best / fps, 3),
        })
    return result


def interpolate(points: list[dict]) -> list[dict]:
    """Fill unobserved plies by spreading them evenly between observed neighbours."""
    known = [(i, p["timestamp"]) for i, p in enumerate(points) if p["timestamp"] is not None]
    if not known:
        raise RuntimeError("No ply could be located in the video")
    filled = [dict(p) for p in points]
    for slot in range(len(known) - 1):
        (i0, t0), (i1, t1) = known[slot], known[slot + 1]
        for k in range(i0 + 1, i1):
            filled[k]["timestamp"] = round(t0 + (t1 - t0) * (k - i0) / (i1 - i0), 3)
            filled[k]["interpolated"] = True
    return filled
