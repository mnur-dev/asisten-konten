"""Read a broadcast's digital overlay board.

The overlay is axis-aligned and rendered by the graphics system, so it can be
located geometrically (strongest 8x8 checkerboard) and then refined against the
PGN itself: the correct rectangle is the one whose squares agree with positions
the game actually reaches.
"""
import numpy as np

from core.video import sample_gray

CHECKER = np.indices((8, 8)).sum(0) % 2 * 2 - 1
PARITY = (np.indices((8, 8)).sum(0) % 2).astype(bool)

GRID = 256
CELL = GRID // 8
MARGIN = 6                # ignore the outer 6px of each 32px cell
OCCUPIED_STD = 20.0       # empty overlay squares are flat; pieces are not
DARK_FRACTION = 0.269     # share of near-black pixels separating black from white
DARK_LEVEL = 70           # only a fallback now; see calibrate_colour()

MIN_SQUARE_CONTRAST = 40  # below this the two board colours are not really two colours
COLOUR_CUTS = np.round(np.arange(0.03, 0.98, 0.02), 3)   # candidate white/black cuts
COLOUR_FRAMES = 96        # frames the colour cut is fitted on

SEARCH_FRAMES = 8         # frames used while searching; the winner is scored on all
COST_CAP = 8              # a frame not showing this board must not dominate the search
# (position radius, size radius, step) per pass: locate_geometric leaves up to ~16px of
# position error and ~32px of size error behind, and each pass homes in on the last
SEARCH_PASSES = ((16, 32, 8), (6, 12, 3), (2, 4, 1))


def _cell_pixels(frames):
    cells = frames.reshape(-1, 8, CELL, 8, CELL).transpose(0, 1, 3, 2, 4)
    inner = cells[:, :, :, MARGIN:CELL - MARGIN, MARGIN:CELL - MARGIN]
    return inner.reshape(len(frames), 8, 8, -1).astype(np.float32)


def occupancy(frames) -> np.ndarray:
    """(n, 8, 8) bool — True where a piece sits."""
    return _cell_pixels(frames).std(-1) > OCCUPIED_STD


def oriented(references, flipped):
    """PGN signatures as the overlay draws them: turned around when it is."""
    return references[:, ::-1, ::-1] if flipped else references


def orientation(frames, references):
    """True when the overlay draws the board from Black's side.

    Broadcasts orient the graphic to the player they are following, so a game whose
    featured player is Black arrives rotated 180 degrees. Measured on Praggnanandhaa-
    Dubov: read upright, 3 of 270 frames matched a position; read turned around, 251
    did. Nothing else in the pipeline notices -- the rectangle is found, the board is
    plainly there, and every ply simply fails to match, which reads as "the overlay is
    showing a different game".

    Decided on occupancy alone, because it is the only signal that does not depend on
    the colour cuts, and those cannot be fitted until the orientation is known. The
    opening is 180-degree symmetric in occupancy and says nothing either way, but the
    middlegame separates the two decisively.
    """
    seen = occupancy(frames)
    wanted = np.abs(references).astype(bool)
    scores = [float(np.minimum(
        (view[:, None] != wanted[None]).sum((2, 3)).min(1), COST_CAP).sum())
        for view in (seen, seen[:, ::-1, ::-1])]
    return scores[1] < scores[0]


def board_levels(pixels, occupied):
    """(light, dark) grey of this overlay's empty squares. None where unmeasurable."""
    parity = np.broadcast_to(PARITY, occupied.shape)
    mean, empty = pixels.mean(-1), ~occupied
    return [float(np.median(mean[mask])) if mask.sum() >= 32 else None
            for mask in (empty & ~parity, empty & parity)]


def _parity_costs(bright, occupied, references, squares):
    """(cuts, frames, plies) disagreements on `squares`, one layer per candidate cut."""
    seen = occupied[:, squares]
    share = bright[:, squares]
    wanted = references[:, squares]
    layers = np.empty((len(COLOUR_CUTS), len(bright), len(references)), np.int16)
    for index, cut in enumerate(COLOUR_CUTS):
        read = np.where(seen, np.where(share >= cut, 1, -1), 0).astype(np.int8)
        layers[index] = (read[:, None] != wanted[None]).sum(-1)
    return layers


def calibrate_colour(frames, references):
    """Learn this overlay's white/black test, and prove it against the PGN.

    Returns (level, light_cut, dark_cut), or None to fall back to the fixed
    DARK_LEVEL/DARK_FRACTION pair.

    The fixed test asks what share of a cell is darker than grey 70, which is a
    question about the board square as much as about the piece. It held for every
    broadcast whose dark squares sat around grey 120-145 and broke on the first one
    that did not: an overlay with dark squares at grey 65 has 75% of an *empty* dark
    square already counting as dark, so every white piece standing on one read black.
    Measured there: 657 misreads, all white-on-dark, none on a light square -- and 33
    of 84 plies observed instead of 76.

    So `level` comes out of the video: midway between this overlay's own two square
    colours. The white/black cut is then fitted per parity, because the background
    contributes to the bright share and the two parities genuinely sit in different
    places (0.50 and 0.10 on that broadcast).

    The cuts are chosen the way the rectangle is -- by agreeing with positions the
    game actually reaches -- and not by clustering the brightness on its own. Both
    unsupervised splits were tried across eleven projects and both place the boundary
    badly whenever one colour outnumbers the other, which is the normal case once
    pieces come off: Otsu and two-means split the *larger* cluster instead of the gap
    (19.6% of cells misread, and one project fell from 124/124 plies to 0/124), while
    a histogram valley still missed by enough to cost 2.7%. An oracle cut sat under 2%
    everywhere, so the feature was never the problem -- picking the number was.

    Because light and dark squares partition the board, each parity's disagreements
    depend on only its own cut, so the two can be costed separately and then added.
    That turns a 47x47 search into two cheap sweeps plus a sum.

    The fixed test stays in the running and wins where it is genuinely better -- it
    still is on some broadcasts, by a square or so per frame -- which is what keeps
    this from being able to regress a video that already reads perfectly.
    """
    pixels = _cell_pixels(frames)
    occupied = pixels.std(-1) > OCCUPIED_STD
    light, dark = board_levels(pixels, occupied)
    if light is None or dark is None or light - dark < MIN_SQUARE_CONTRAST:
        return None
    level = (light + dark) / 2.0
    step = max(1, len(frames) // COLOUR_FRAMES)
    bright = (pixels > level).mean(-1)[::step].reshape(-1, 64)
    seen = occupied[::step].reshape(-1, 64)
    wanted = references.reshape(len(references), 64)
    flat = PARITY.reshape(64)
    on_light = _parity_costs(bright, seen, wanted, ~flat)
    on_dark = _parity_costs(bright, seen, wanted, flat)

    best, winners = None, []
    for i in range(len(COLOUR_CUTS)):
        for j in range(len(COLOUR_CUTS)):
            total = float(np.minimum((on_light[i] + on_dark[j]).min(1), COST_CAP).sum())
            if best is None or total < best:
                best, winners = total, [(i, j)]
            elif total == best:
                winners.append((i, j))
    fixed = signatures(frames[::step]).reshape(-1, 64)
    fixed_cost = float(np.minimum(
        (fixed[:, None] != wanted[None]).sum(-1).min(1), COST_CAP).sum())
    if best is None or fixed_cost <= best:
        return None
    # Once every informative frame reads exactly, the cost bottoms out and a whole
    # plateau of cuts ties. Sitting on its edge is one bad frame away from flipping a
    # piece, so aim for the middle of the plateau instead of whichever pair the loops
    # happened to reach first -- then snap to the nearest pair that actually won, since
    # the plateau need not be a rectangle and its centre need not lie inside it.
    middle = (np.median([i for i, _ in winners]), np.median([j for _, j in winners]))
    i, j = min(winners, key=lambda pair: abs(pair[0] - middle[0]) + abs(pair[1] - middle[1]))
    return level, float(COLOUR_CUTS[i]), float(COLOUR_CUTS[j])


def signatures(frames, calibration=None) -> np.ndarray:
    """(n, 8, 8) of {0 empty, +1 white, -1 black}."""
    pixels = _cell_pixels(frames)
    occupied = pixels.std(-1) > OCCUPIED_STD
    if calibration is None:
        dark = (pixels < DARK_LEVEL).mean(-1)
        white = dark < DARK_FRACTION
    else:
        level, light_threshold, dark_threshold = calibration
        bright = (pixels > level).mean(-1)
        parity = np.broadcast_to(PARITY, occupied.shape)
        white = bright >= np.where(parity, dark_threshold, light_threshold)
    return np.where(occupied, np.where(white, 1, -1), 0).astype(np.int8)


def _block_means(image, block):
    """Mean of every block x block window, via a summed-area table."""
    integral = np.zeros((image.shape[0] + 1, image.shape[1] + 1), np.float64)
    np.cumsum(np.cumsum(image, 0), 1, out=integral[1:, 1:])
    total = (integral[block:, block:] - integral[:-block, block:]
             - integral[block:, :-block] + integral[:-block, :-block])
    return total / (block * block)


def _correlate_grid(means):
    """For every top-left offset, the checkerboard correlation of the 8x8 square means."""
    height, width = means.shape
    span = 7 * 8
    if height <= span or width <= span:
        return None, None, None
    view = (height - span, width - span)
    signal = np.zeros(view, np.float64)
    total = np.zeros(view, np.float64)
    squares = np.zeros(view, np.float64)
    for row in range(8):
        for col in range(8):
            tile = means[row * 8: row * 8 + view[0], col * 8: col * 8 + view[1]]
            signal += CHECKER[row, col] * tile
            total += tile
            squares += tile * tile
    variance = np.maximum(squares - total * total / 64.0, 1e-6)
    return np.abs(signal) / np.sqrt(variance), None, None


def locate_geometric(frames, min_side=176, max_side=880, coarse=8):
    """Find the strongest 8x8 checkerboard at any position and scale.

    Each candidate side is tested by rescaling the image so that a board would be
    exactly 64px, which turns the search into a handful of vectorised array ops.
    """
    from PIL import Image

    image = np.median(frames, axis=0).astype(np.float32)
    source = Image.fromarray(image.astype(np.uint8))
    height, width = image.shape
    best = (-9.0, None)
    upper = min(max_side, min(height, width))
    for side in range(min_side, upper + 1, coarse):
        scale = 64.0 / side
        small = np.asarray(source.resize((max(72, int(width * scale)),
                                          max(72, int(height * scale))),
                                         Image.BILINEAR), np.float32)
        means = _block_means(small, 8)
        grid, _, _ = _correlate_grid(means)
        if grid is None:
            continue
        index = int(grid.argmax())
        y, x = divmod(index, grid.shape[1])
        score = float(grid[y, x])
        if score > best[0]:
            best = (score, (int(round(x / scale)), int(round(y / scale)), side))
    if best[1] is None:
        raise RuntimeError("No checkerboard-like region found")
    return best


def scale_window(frames, box):
    """Crop an (x, y, side) box out of already-cropped frames and rescale it to GRID.

    This resamples with PIL while detect() reads its winner through ffmpeg's scaler,
    so the two paths are close but not identical -- measured on one video, the same
    rectangle matched a position exactly in 259 of 494 frames here and 230 there. What
    they must agree on is the geometry, and they only do because sample_gray converts
    to gray before cropping; left in yuv420p, ffmpeg would snap an odd offset onto the
    chroma grid and read a rectangle two pixels from the one scored here.
    """
    from PIL import Image
    x, y, side = box
    return np.array([np.asarray(Image.fromarray(frame[y:y + side, x:x + side])
                                .resize((GRID, GRID), Image.BILINEAR))
                     for frame in frames])


def read_window(frames, box, calibration=None):
    """Signatures for an (x, y, side) window of already-cropped frames."""
    return signatures(scale_window(frames, box), calibration)


def refine_against_pgn(video, rect, references, samples=18, pad=40, frame_size=None,
                       window=None):
    """Nudge the rectangle so that the board reads as positions the game reaches.

    A rectangle over unrelated graphics matches nothing, which is what proves a
    candidate. `window` is (start_seconds, length_seconds) to test only that stretch:
    a broadcast that moves its overlay partway through has a rectangle which is right
    for part of the timeline and meaningless for the rest, so proving it against the
    whole video would understate a layout that is genuinely correct while it is on
    screen. Returns (rect, flipped, exact_matches, frames_tested), where `flipped`
    says the overlay draws the board from Black's side.

    Two things here were learned the hard way, both from the same video (Carlsen-
    Niemann, where the detector settled 10px off and only 14 of 71 plies were ever
    read directly, the rest interpolated):

    Score on the full signature, not on occupancy. A board read half a square off
    still has a piece somewhere inside almost every occupied cell, so occupancy
    barely notices the slip -- but the cell then holds as much square as piece, and
    the light/dark test flips. Colour is the signal that actually pins the alignment.

    Search a grid, not one axis at a time. The old objective (how many frames match
    some position exactly) is a step function: from a rectangle that is wrong in two
    axes at once, no single-axis move improves it, so a coordinate descent stops on
    the plateau it started on. Summing each frame's best cost gives a slope to
    follow everywhere, and a coarse-then-fine grid does not need the axes to be
    independent.
    """
    x0, y0, side0 = rect
    px, py = max(0, x0 - pad), max(0, y0 - pad)
    span = side0 + 2 * pad
    if frame_size:
        span = min(span, frame_size[0] - px, frame_size[1] - py)
    start, length = window if window else (None, _duration_hint(video))
    frames = sample_gray(video, samples / max(length, 1e-6), (span, span),
                         crop=(px, py, span), start=start,
                         count=samples if window else None)
    offset_x, offset_y = x0 - px, y0 - py
    searching = frames[::max(1, len(frames) // SEARCH_FRAMES)]
    # Orientation first: it is decided on occupancy, which needs no cuts, and the cuts
    # cannot be fitted until it is known -- fitted against a board read upside down,
    # they would be fitted to noise.
    start_box = scale_window(frames, (offset_x, offset_y, side0))
    flipped = orientation(start_box, references)
    wanted = oriented(references, flipped)
    # Learned once, from the starting rectangle, and then held fixed for every
    # candidate. Re-learning it per candidate would let a rectangle that frames
    # something else pick cuts that flatter its own garbage, and the whole point of
    # this search is that a wrong rectangle agrees with nothing.
    calibration = calibrate_colour(start_box, wanted)

    def score(dx, dy, dside, on=None):
        """(summed best-ply cost, frames matching a position exactly); lower cost wins."""
        side = side0 + dside
        ox, oy = offset_x + dx, offset_y + dy
        if side < 64 or ox < 0 or oy < 0 or ox + side > span or oy + side > span:
            return None
        read = read_window(searching if on is None else on, (ox, oy, side), calibration)
        cost = (read[:, None] != wanted[None]).sum((2, 3)).min(1)
        return float(np.minimum(cost, COST_CAP).sum()), int((cost == 0).sum())

    def grid(centre, radii, steps):
        best, at = None, centre
        for dside in _around(centre[2], radii[2], steps[2]):
            for dx in _around(centre[0], radii[0], steps[0]):
                for dy in _around(centre[1], radii[1], steps[1]):
                    found = score(dx, dy, dside)
                    if found is None:
                        continue
                    # Ties go to the smallest move. The cost bottoms out at zero once
                    # every informative frame reads exactly, so a whole plateau of
                    # rectangles scores the same -- and without this the winner is
                    # whichever the loops happened to reach first, which is how a
                    # perfectly good estimate drifts 25px and takes the reading with it.
                    key = (found[0], abs(dx) + abs(dy) + abs(dside))
                    if best is None or key < best:
                        best, at = key, (dx, dy, dside)
        return at

    at = (0, 0, 0)
    for shift, size, step in SEARCH_PASSES:
        at = grid(at, (shift, shift, size), (step, step, step))
    dx, dy, dside = at
    # The cuts were fitted where the search started, up to ~16px off; refit them on
    # the rectangle it settled on, since this count is what the caller gates on and
    # is also what detect() will read the whole video under.
    final = scale_window(frames, (offset_x + dx, offset_y + dy, side0 + dside))
    calibration = calibrate_colour(final, wanted) or calibration
    read = signatures(final, calibration)
    exact = int(((read[:, None] != wanted[None]).sum((2, 3)).min(1) == 0).sum())
    return (x0 + dx, y0 + dy, side0 + dside), flipped, exact, len(frames)


def _around(centre, radius, step):
    return range(centre - radius, centre + radius + 1, step)


def _duration_hint(video):
    from core.video import probe
    return probe(video)["duration"]
