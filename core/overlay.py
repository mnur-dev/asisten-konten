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
DARK_LEVEL = 70


def _cell_pixels(frames):
    cells = frames.reshape(-1, 8, CELL, 8, CELL).transpose(0, 1, 3, 2, 4)
    inner = cells[:, :, :, MARGIN:CELL - MARGIN, MARGIN:CELL - MARGIN]
    return inner.reshape(len(frames), 8, 8, -1).astype(np.float32)


def occupancy(frames) -> np.ndarray:
    """(n, 8, 8) bool — True where a piece sits."""
    return _cell_pixels(frames).std(-1) > OCCUPIED_STD


def signatures(frames) -> np.ndarray:
    """(n, 8, 8) of {0 empty, +1 white, -1 black}."""
    pixels = _cell_pixels(frames)
    occupied = pixels.std(-1) > OCCUPIED_STD
    dark = (pixels < DARK_LEVEL).mean(-1)
    return np.where(occupied, np.where(dark < DARK_FRACTION, 1, -1), 0).astype(np.int8)


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


def occupancy_of_crop(crop):
    """Occupancy for an (n, side, side) native-resolution crop, without rescaling."""
    side = crop.shape[1]
    step = side / 8.0
    out = np.empty((len(crop), 8, 8), bool)
    for row in range(8):
        for col in range(8):
            y0, y1 = int(row * step), int((row + 1) * step)
            x0, x1 = int(col * step), int((col + 1) * step)
            inset = max(1, int((y1 - y0) * MARGIN / CELL))
            cell = crop[:, y0 + inset:y1 - inset, x0 + inset:x1 - inset]
            out[:, row, col] = cell.reshape(len(crop), -1).astype(np.float32).std(-1) > OCCUPIED_STD
    return out


def refine_against_pgn(video, rect, references, samples=18, pad=40, frame_size=None):
    """Nudge the rectangle so that as many frames as possible match a real position.

    The correct rectangle is the one under which the board reads as positions the
    game actually reaches; a rectangle over unrelated graphics matches nothing.
    Returns (rect, exact_matches, frames_tested).
    """
    reference_occupancy = references != 0
    x0, y0, side0 = rect
    px, py = max(0, x0 - pad), max(0, y0 - pad)
    span = side0 + 2 * pad
    if frame_size:
        span = min(span, frame_size[0] - px, frame_size[1] - py)
    frames = sample_gray(video, samples / _duration_hint(video), (span, span),
                         crop=(px, py, span))
    offset_x, offset_y = x0 - px, y0 - py

    def score(dx, dy, dside):
        side = side0 + dside
        ox, oy = offset_x + dx, offset_y + dy
        if side < 64 or ox < 0 or oy < 0 or ox + side > span or oy + side > span:
            return -1
        crop = frames[:, oy:oy + side, ox:ox + side]
        cost = (occupancy_of_crop(crop)[:, None] != reference_occupancy[None]).sum((2, 3))
        return int((cost.min(1) == 0).sum())

    dx = dy = dside = 0
    best = score(0, 0, 0)
    for _ in range(3):
        for axis, radius, step in ((2, 30, 3), (0, 16, 2), (1, 16, 2)):
            for delta in range(-radius, radius + 1, step):
                trial = [dx, dy, dside]
                trial[axis] = [dx, dy, dside][axis] + delta
                value = score(*trial)
                if value > best:
                    best, (dx, dy, dside) = value, tuple(trial)
    return (x0 + dx, y0 + dy, side0 + dside), best, len(frames)


def _duration_hint(video):
    from core.video import probe
    return probe(video)["duration"]
