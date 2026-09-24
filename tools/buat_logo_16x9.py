"""Turn the square channel avatars into the 16:9 logo plates used in the layout step.

The YouTube avatars are 1:1, so dropped into the wide box someone draws over the
broadcast they only fill its middle and the box looks empty either side. This pads
each one out to 16:9 by continuing its OWN background -- a flat fill where the
avatar has one, the checkerboard carried on where it has that -- so the pad reads
as part of the logo rather than as a bar stuck to it.

    .venv/bin/python tools/buat_logo_16x9.py

Writes assets/brand/<slug>.png. Re-run after replacing an avatar in app/ui/channels.
"""
import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SOURCES = ROOT / "app" / "ui" / "channels"
TARGET = ROOT / "assets" / "brand"
RATIO = 16 / 9


TOLERANCE = 24            # per channel, what still counts as "the same colour"
AGREEMENT = 0.7           # share of samples that must sit within it


def _median(samples):
    return tuple(sorted(px[channel] for px in samples)[len(samples) // 2]
                 for channel in range(3))


def _agrees(samples, colour):
    """The share of `samples` within TOLERANCE of `colour` on every channel. Taken
    against the median rather than the first sample: the avatars' artwork reaches
    into the border here and there (a flag, the glow around a letter), and one such
    sample landing first would otherwise decide the whole read."""
    close = sum(1 for px in samples
                if max(abs(a - b) for a, b in zip(px[:3], colour)) <= TOLERANCE)
    return close / len(samples)


def border_samples(image, step=4):
    w, h = image.size
    return ([image.getpixel((x, 1)) for x in range(0, w, step)]
            + [image.getpixel((x, h - 2)) for x in range(0, w, step)]
            + [image.getpixel((1, y)) for y in range(0, h, step)]
            + [image.getpixel((w - 2, y)) for y in range(0, h, step)])


def edge_colour(image):
    """The avatar's background, read off its border ring -- and None when the ring
    isn't mostly one colour, which is how a patterned background announces itself."""
    ring = border_samples(image)
    colour = _median(ring)
    return colour if _agrees(ring, colour) >= AGREEMENT else None


def checker_of(image, cell=100):
    """(light, dark) if the avatar's background is a two-colour checkerboard on a
    `cell` grid, else None. Sampled at the centre of every cell around the border,
    which is where the artwork is thinnest."""
    w, h = image.size
    if w % cell or h % cell:
        return None
    cols, rows = w // cell, h // cell
    edge = ([(col, 0) for col in range(cols)] + [(col, rows - 1) for col in range(cols)]
            + [(0, row) for row in range(rows)] + [(cols - 1, row) for row in range(rows)])
    groups = ([], [])
    for col, row in edge:
        px = image.getpixel((col * cell + cell // 2, row * cell + cell // 2))
        groups[(col + row) % 2].append(px)
    if not all(groups):
        return None
    light, dark = (_median(group) for group in groups)
    if any(_agrees(group, colour) < AGREEMENT
           for group, colour in zip(groups, (light, dark))):
        return None
    if max(abs(a - b) for a, b in zip(light, dark)) < 40:
        return None       # one flat colour read twice, not a checkerboard
    return light, dark


def checker_ground(size, cell, colours, origin):
    """A checkerboard filling `size`, its grid aligned so that the avatar pasted at
    `origin` continues the pattern instead of restarting it."""
    light, dark = colours
    ground = Image.new("RGB", size, dark)
    ox, oy = origin
    w, h = size
    col = -((ox + cell - 1) // cell)          # first whole cell left of the avatar
    while col * cell + ox < w:
        row = -((oy + cell - 1) // cell)
        while row * cell + oy < h:
            if (col + row) % 2 == 0:
                x, y = ox + col * cell, oy + row * cell
                ground.paste(light, (x, y, x + cell, y + cell))
            row += 1
        col += 1
    return ground


def widen(source: Path, target: Path):
    avatar = Image.open(source).convert("RGB")
    w, h = avatar.size
    width = round(h * RATIO / 2) * 2
    origin = ((width - w) // 2, 0)
    if colours := checker_of(avatar):
        ground = checker_ground((width, h), 100, colours, origin)
        how = f"checkerboard {colours[0]}/{colours[1]}"
    else:
        fill = edge_colour(avatar) or (0, 0, 0)
        ground = Image.new("RGB", (width, h), fill)
        how = f"flat {fill}"
    ground.paste(avatar, origin)
    target.parent.mkdir(parents=True, exist_ok=True)
    ground.save(target)
    print(f"{target.relative_to(ROOT)}  {w}x{h} -> {width}x{h}  ({how})")


if __name__ == "__main__":
    for avatar in sorted(SOURCES.glob("*.jpg")):
        widen(avatar, TARGET / f"{avatar.stem}.png")
