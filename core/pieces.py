"""Rasterise the cburnett piece set that ships with python-chess.

python-chess carries the pieces as SVG, but every off-the-shelf SVG rasteriser on
Windows wants a cairo DLL that the pycairo wheel only exposes to itself. pycairo
does work, so the shortest honest path is to walk the SVG and drive cairo
directly. The pieces use a small, fixed subset of SVG, so this stays short.
"""
import math
import re
import xml.etree.ElementTree as ET
from functools import lru_cache
from pathlib import Path

import cairo
import chess
import chess.svg
from PIL import Image

NUMBER = re.compile(r"[-+]?(?:\d*\.\d+|\d+)(?:[eE][-+]?\d+)?")
COMMAND = re.compile(r"([MmLlHhVvCcSsQqTtAaZz])")
VIEWBOX = 45.0          # cburnett pieces are drawn on a 45x45 grid

BUNDLED = "cburnett"
ASSETS = Path(__file__).parents[1] / "assets" / "pieces"


def available_sets() -> list[str]:
    """The bundled set, plus any dropped into assets/pieces/<name>/wN.svg."""
    found = [BUNDLED]
    if ASSETS.is_dir():
        found += sorted(directory.name for directory in ASSETS.iterdir()
                        if directory.is_dir() and any(directory.glob("*.svg")))
    return found


def _svg_source(symbol: str, piece_set: str) -> str:
    if piece_set and piece_set != BUNDLED:
        name = ("w" if symbol.isupper() else "b") + symbol.upper()
        for candidate in (ASSETS / piece_set / f"{name}.svg",
                          ASSETS / piece_set / f"{name.lower()}.svg"):
            if candidate.is_file():
                return candidate.read_text(encoding="utf-8")
    return chess.svg.piece(chess.Piece.from_symbol(symbol))


def _numbers(text):
    return [float(v) for v in NUMBER.findall(text)]


def _colour(value, inherited):
    value = (value or inherited or "none").strip()
    if value in ("none", ""):
        return None
    if value.startswith("#"):
        digits = value[1:]
        if len(digits) == 3:
            digits = "".join(c * 2 for c in digits)
        return tuple(int(digits[i:i + 2], 16) / 255 for i in (0, 2, 4))
    return {"white": (1, 1, 1), "black": (0, 0, 0)}.get(value, (0, 0, 0))


def _style(element):
    out = dict(element.attrib)
    for pair in element.get("style", "").split(";"):
        if ":" in pair:
            key, value = pair.split(":", 1)
            out[key.strip()] = value.strip()
    return out


def _arc(context, x0, y0, rx, ry, rotation, large, sweep, x, y):
    """SVG elliptical arc -> cairo, via the usual endpoint-to-centre conversion."""
    if rx == 0 or ry == 0:
        context.line_to(x, y)
        return
    phi = math.radians(rotation)
    dx2, dy2 = (x0 - x) / 2.0, (y0 - y) / 2.0
    x1 = math.cos(phi) * dx2 + math.sin(phi) * dy2
    y1 = -math.sin(phi) * dx2 + math.cos(phi) * dy2
    rx, ry = abs(rx), abs(ry)
    check = x1 * x1 / (rx * rx) + y1 * y1 / (ry * ry)
    if check > 1:
        rx, ry = rx * math.sqrt(check), ry * math.sqrt(check)
    denominator = rx * rx * y1 * y1 + ry * ry * x1 * x1
    factor = 0.0 if denominator == 0 else max(
        0.0, (rx * rx * ry * ry - denominator) / denominator)
    coefficient = math.sqrt(factor) * (-1 if large == sweep else 1)
    cx1, cy1 = coefficient * rx * y1 / ry, -coefficient * ry * x1 / rx
    cx = math.cos(phi) * cx1 - math.sin(phi) * cy1 + (x0 + x) / 2.0
    cy = math.sin(phi) * cx1 + math.cos(phi) * cy1 + (y0 + y) / 2.0
    start = math.atan2((y1 - cy1) / ry, (x1 - cx1) / rx)
    end = math.atan2((-y1 - cy1) / ry, (-x1 - cx1) / rx)
    context.save()
    context.translate(cx, cy)
    context.rotate(phi)
    context.scale(rx, ry)
    (context.arc if sweep else context.arc_negative)(0, 0, 1, start, end)
    context.restore()


def _path(context, data):
    tokens = [t for t in COMMAND.split(data) if t.strip()]
    x = y = start_x = start_y = 0.0
    previous_control = None
    command = None
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if COMMAND.fullmatch(token):
            command = token
            index += 1
            values = _numbers(tokens[index]) if index < len(tokens) and \
                not COMMAND.fullmatch(tokens[index]) else []
            if values:
                index += 1
        else:
            values = _numbers(token)
            index += 1
        relative = command.islower()
        upper = command.upper()
        if upper == "Z":
            context.close_path()
            x, y = start_x, start_y
            previous_control = None
            continue
        step = {"M": 2, "L": 2, "H": 1, "V": 1, "C": 6, "S": 4, "Q": 4, "T": 2, "A": 7}[upper]
        for offset in range(0, len(values) - step + 1, step):
            chunk = values[offset:offset + step]
            if upper in ("M", "L"):
                nx, ny = (x + chunk[0], y + chunk[1]) if relative else (chunk[0], chunk[1])
                if upper == "M" and offset == 0:
                    context.move_to(nx, ny)
                    start_x, start_y = nx, ny
                    command = "l" if relative else "L"
                else:
                    context.line_to(nx, ny)
                x, y = nx, ny
                previous_control = None
            elif upper == "H":
                x = x + chunk[0] if relative else chunk[0]
                context.line_to(x, y)
                previous_control = None
            elif upper == "V":
                y = y + chunk[0] if relative else chunk[0]
                context.line_to(x, y)
                previous_control = None
            elif upper in ("C", "S"):
                if upper == "C":
                    c1 = (x + chunk[0], y + chunk[1]) if relative else (chunk[0], chunk[1])
                    c2 = (x + chunk[2], y + chunk[3]) if relative else (chunk[2], chunk[3])
                    end = (x + chunk[4], y + chunk[5]) if relative else (chunk[4], chunk[5])
                else:
                    c1 = (2 * x - previous_control[0], 2 * y - previous_control[1]) \
                        if previous_control else (x, y)
                    c2 = (x + chunk[0], y + chunk[1]) if relative else (chunk[0], chunk[1])
                    end = (x + chunk[2], y + chunk[3]) if relative else (chunk[2], chunk[3])
                context.curve_to(*c1, *c2, *end)
                previous_control = c2
                x, y = end
            elif upper in ("Q", "T"):
                if upper == "Q":
                    q = (x + chunk[0], y + chunk[1]) if relative else (chunk[0], chunk[1])
                    end = (x + chunk[2], y + chunk[3]) if relative else (chunk[2], chunk[3])
                else:
                    q = (2 * x - previous_control[0], 2 * y - previous_control[1]) \
                        if previous_control else (x, y)
                    end = (x + chunk[0], y + chunk[1]) if relative else (chunk[0], chunk[1])
                context.curve_to(x + 2 / 3 * (q[0] - x), y + 2 / 3 * (q[1] - y),
                                 end[0] + 2 / 3 * (q[0] - end[0]),
                                 end[1] + 2 / 3 * (q[1] - end[1]), *end)
                previous_control = q
                x, y = end
            elif upper == "A":
                end = (x + chunk[5], y + chunk[6]) if relative else (chunk[5], chunk[6])
                _arc(context, x, y, chunk[0], chunk[1], chunk[2],
                     int(chunk[3]), int(chunk[4]), *end)
                x, y = end
                previous_control = None


JOINS = {"miter": cairo.LINE_JOIN_MITER, "round": cairo.LINE_JOIN_ROUND,
         "bevel": cairo.LINE_JOIN_BEVEL}
CAPS = {"butt": cairo.LINE_CAP_BUTT, "round": cairo.LINE_CAP_ROUND,
        "square": cairo.LINE_CAP_SQUARE}


def _draw(context, element, inherited):
    style = _style(element)
    state = {key: style.get(key, inherited.get(key))
             for key in ("fill", "stroke", "stroke-width", "stroke-linecap",
                         "stroke-linejoin", "fill-rule")}
    tag = element.tag.split("}")[-1]

    context.save()
    if transform := style.get("transform"):
        if values := _numbers(transform):
            if transform.strip().startswith("translate"):
                context.translate(values[0], values[1] if len(values) > 1 else 0)

    if tag in ("path", "circle"):
        context.new_path()
        if tag == "circle":
            context.arc(float(style.get("cx", 0)), float(style.get("cy", 0)),
                        float(style.get("r", 0)), 0, 2 * math.pi)
        else:
            _path(context, style.get("d", ""))
        context.set_fill_rule(cairo.FILL_RULE_EVEN_ODD
                              if state.get("fill-rule") == "evenodd"
                              else cairo.FILL_RULE_WINDING)
        fill = _colour(state.get("fill"), None)
        stroke = _colour(state.get("stroke"), None)
        if fill:
            context.set_source_rgb(*fill)
            context.fill_preserve() if stroke else context.fill()
        if stroke:
            context.set_source_rgb(*stroke)
            context.set_line_width(float(state.get("stroke-width") or 1.5))
            context.set_line_join(JOINS.get(state.get("stroke-linejoin"), cairo.LINE_JOIN_MITER))
            context.set_line_cap(CAPS.get(state.get("stroke-linecap"), cairo.LINE_CAP_BUTT))
            context.stroke()
        context.new_path()

    for child in element:
        _draw(context, child, state)
    context.restore()


@lru_cache(maxsize=256)
def piece_image(symbol: str, size: int, piece_set: str = BUNDLED) -> Image.Image:
    """RGBA image of one piece, drawn to fill a `size` x `size` square."""
    root = ET.fromstring(_svg_source(symbol, piece_set))
    span = VIEWBOX
    if box := root.get("viewBox"):
        values = _numbers(box)
        if len(values) == 4 and values[2] > 0:
            span = max(values[2], values[3])
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
    context = cairo.Context(surface)
    context.scale(size / span, size / span)
    for child in root:
        _draw(context, child, {})
    surface.flush()
    image = Image.frombuffer("RGBA", (size, size), bytes(surface.get_data()),
                             "raw", "BGRa", surface.get_stride(), 1)
    return image.convert("RGBA")


def available(piece_set: str = BUNDLED) -> bool:
    try:
        piece_image("N", 32, piece_set)
        return True
    except Exception:
        return False
