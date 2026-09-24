"""Render a board video whose ply durations follow detected timestamps.

One PNG per ply plus FFmpeg's concat demuxer, so encoding cost scales with the
number of moves rather than the number of output frames.
"""
import logging
import math
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import chess
from PIL import Image, ImageColor, ImageDraw, ImageFilter, ImageFont

from core import pgn
from core.video import probe

log = logging.getLogger(__name__)

GLYPHS = {"P": "♙", "N": "♘", "B": "♗", "R": "♖", "Q": "♕", "K": "♔",
          "p": "♟", "n": "♞", "b": "♝", "r": "♜", "q": "♛", "k": "♚"}

# light, dark, highlight-on-light, highlight-on-dark, page background
THEMES = {
    "orange":   ("#ebecd0", "#e98839", "#cfc751", "#c48a20", "#262421"),
    "chesscom": ("#ebecd0", "#739552", "#f7f769", "#b9ca43", "#302e2b"),
    "blue":     ("#dee3e6", "#8ca2ad", "#cdd26a", "#aaa23b", "#22272b"),
    "wood":     ("#f0d9b5", "#b58863", "#f7ec74", "#dac431", "#2b2622"),
    "slate":    ("#e6e9ee", "#4a5568", "#e9c46a", "#b98a2f", "#171a20"),
}
DEFAULT_THEME = "orange"

# seconds the board stays frozen on its final position after the last move

# frame rate render() writes board.mp4 at -- overlay_composite snaps its start to
# this grid so the seek lands on a whole frame instead of between two
BOARD_FPS = 30

# the clock strip under the board, measured in squares: plate height, and the gap
# between the board's bottom edge and the plates
CLOCK_HEIGHT = 0.34
CLOCK_GAP = 0.12


def theme_colours(name):
    return THEMES.get(name or DEFAULT_THEME, THEMES[DEFAULT_THEME])


def castling_squares(board, move):
    """(king destination, rook origin, rook destination) if `move` castles, else None.

    Worked out from the position rather than from a table of the four standard
    castles, because Freestyle/960 starts the king and rooks on other files -- and
    encodes the move as king-takes-rook, so the move's own target square is the rook.
    Both flavours still land on the same files: g/f kingside, c/d queenside.
    """
    if not board.is_castling(move):
        return None
    kingside = board.is_kingside_castling(move)
    rank = chess.square_rank(move.from_square)
    rook_from = move.to_square if board.chess960 else chess.square(7 if kingside else 0, rank)
    return (chess.square(6 if kingside else 2, rank), rook_from,
            chess.square(5 if kingside else 3, rank))


def _square_pixel(index, ox, oy, square, orientation):
    """Top-left pixel of a square's box, matching board_image's own layout."""
    file, rank = chess.square_file(index), chess.square_rank(index)
    col, row = (file, 7 - rank) if orientation == "white" else (7 - file, rank)
    return ox + col * square, oy + row * square


def fit_size(square_px=135, evaluation=False, clocks=False, margin=0.06):
    """Canvas sized tightly around the board (plus the eval bar strip and, with
    `clocks`, the clock plates under it), no letterboxing. Rounded up to even
    dimensions, which yuv420p requires."""
    bar_width = max(10, int(square_px * 0.42)) if evaluation else 0
    gap = max(4, int(square_px * 0.18)) if evaluation else 0
    side = square_px * 8
    strip = int(square_px * (CLOCK_HEIGHT + CLOCK_GAP)) if clocks else 0
    width = int((side + bar_width + gap) / (1 - margin))
    height = int((side + strip) / (1 - margin))
    return width + width % 2, height + height % 2


def find_font():
    candidates = []
    if windir := os.environ.get("WINDIR"):
        candidates.append(Path(windir) / "Fonts" / "seguisym.ttf")
    candidates += [Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
                   Path("/System/Library/Fonts/Apple Symbols.ttf")]
    for font in candidates:
        if font.is_file():
            return font
    raise RuntimeError("No Unicode chess font found")


def fitted_font(draw, text, max_width, ceiling):
    """Largest font size at which `text` still fits inside the bar."""
    path = str(find_font())
    for size in range(ceiling, 6, -1):
        font = ImageFont.truetype(path, size)
        if draw.textlength(text, font=font) <= max_width:
            return font
    return ImageFont.truetype(path, 7)


def find_text_font():
    """A prose font for player names -- find_font() returns a symbol face, which is
    built for chess glyphs and renders names poorly."""
    candidates = []
    if windir := os.environ.get("WINDIR"):
        fonts = Path(windir) / "Fonts"
        candidates += [fonts / "segoeuib.ttf", fonts / "segoeui.ttf",
                       fonts / "arialbd.ttf", fonts / "arial.ttf"]
    candidates += [Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
                   Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
                   Path("/System/Library/Fonts/Helvetica.ttc")]
    for font in candidates:
        if font.is_file():
            return font
    return find_font()


FONTS_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts"


def find_short_font():
    """The face the short captions are set in: Noto Sans Black, bundled in
    assets/fonts so every machine renders the same weight.

    Shipped with the repo rather than looked up through fontconfig because the
    Black weight isn't in Debian's fonts-noto-core -- on the server the family
    resolves to Noto Sans Mono only, and a system lookup would silently land on
    the DejaVu fallback with captions a weight lighter than the ones reviewed in
    the browser."""
    noto = FONTS_DIR / "NotoSans-Black.ttf"
    return noto if noto.is_file() else find_caption_font()


def find_caption_font():
    """The bold condensed face memes use for impact captions -- thick enough to read
    under a heavy stroke outline at small sizes, which a normal-weight prose font
    isn't. Falls back to find_text_font()'s bold sans if Impact isn't installed."""
    if windir := os.environ.get("WINDIR"):
        impact = Path(windir) / "Fonts" / "impact.ttf"
        if impact.is_file():
            return impact
    return find_text_font()


def boxed_font(draw, text, box_w, box_h, font_path):
    """Largest size at which `text` fits the box in BOTH axes. fitted_font() only
    constrains width, which is fine for the eval bar's short labels but lets a long
    player name overflow a short nameplate vertically."""
    path = str(font_path)
    for size in range(max(8, int(box_h)), 7, -1):
        font = ImageFont.truetype(path, size)
        left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
        if right - left <= box_w and bottom - top <= box_h:
            return font
    return ImageFont.truetype(path, 8)


def wrap_lines(draw, text, font, max_width):
    """Greedy word-wrap: pack words onto a line until the next one would overflow."""
    words = text.split()
    lines, line = [], ""
    for word in words:
        candidate = f"{line} {word}".strip()
        if not line or draw.textlength(candidate, font=font) <= max_width:
            line = candidate
        else:
            lines.append(line)
            line = word
    if line:
        lines.append(line)
    return lines or [""]


def fit_wrapped_font(draw, text, box_w, box_h, font_path, ceiling=140, scale=1.0):
    """Largest size at which `text`, wrapped to `box_w`, still fits `box_h` -- a
    multi-line version of boxed_font() for captions rather than one-line labels.
    `scale` then shrinks/grows that auto-fit size by a user-chosen factor (the
    caption's own "text size" control) -- 1.0 leaves the auto-fit size untouched;
    pushed above 1.0 the text can spill past the box, which is the point of letting
    someone size it up on purpose rather than always being capped to fit."""
    path = str(font_path)
    best = 10
    for size in range(ceiling, 9, -2):
        font = ImageFont.truetype(path, size)
        lines = wrap_lines(draw, text, font, box_w)
        line_height = (draw.textbbox((0, 0), "Ag", font=font)[3]) * 1.15
        if line_height * len(lines) <= box_h and all(
                draw.textlength(line, font=font) <= box_w for line in lines):
            best = size
            break
    final_size = max(6, round(best * scale))
    font = ImageFont.truetype(path, final_size)
    return font, wrap_lines(draw, text, font, box_w)


def short_text_layer(size, texts):
    """A full-canvas RGBA image with each user caption drawn into its own rect,
    word-wrapped and auto-fit -- the short-clip equivalent of furniture_layer.
    `texts` is [{"text": str, "rect": (x, y, w, h), "scale": float}, ...]; `scale`
    (default 1.0) is the caption's text-size control, applied on top of the
    auto-fit size. Styled after the meme caption look: Noto Sans Black, yellow
    fill, thick black outline, and a halo of the fill colour behind the lot so
    the caption separates from a busy broadcast frame.
    """
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    font_path = find_short_font()
    for item in texts or []:
        text = (item.get("text") or "").strip()
        if not text:
            continue
        x, y, w, h = item["rect"]
        # extra margin (vs. boxed_font's ~0.92) leaves room for the heavy stroke
        # outline below, which textlength()/fit_wrapped_font() don't account for
        pad_w, pad_h = w * 0.84, h * 0.84
        scale = item.get("scale") or 1.0
        font, lines = fit_wrapped_font(draw, text, pad_w, pad_h, font_path, scale=scale)
        line_height = (draw.textbbox((0, 0), "Ag", font=font)[3]) * 1.05
        stroke = max(3, round(font.size * 0.11))
        top = y + h / 2 - line_height * len(lines) / 2
        fill = item.get("fill") or "#ffe100"
        placed = [((x + w / 2, top + line_height * (index + 0.5)), line)
                  for index, line in enumerate(lines)]
        layer.alpha_composite(text_glow(size, placed, font, fill, stroke))
        for point, line in placed:
            draw.text(point, line, font=font, anchor="mm", fill=fill,
                      stroke_width=stroke, stroke_fill="#000000")
    return layer


# The glow is painted as the text again, spread by a blur: `GLOW_SPREAD` is how far
# past the black outline it reaches (in stroke widths) and `GLOW_PASSES` how many
# times that blurred copy is stacked. Compared side by side on a 1080x1920 caption:
# one pass disappears under the outline, and from spread 1.6 x3 upwards the halo
# closes the gap between two wrapped lines and reads as a yellow slab rather than a
# glow. 1.4 x2 stays clear of the line above while the black outline holds the edge.
GLOW_SPREAD = 1.4
GLOW_PASSES = 2


def text_glow(size, placed, font, colour, stroke):
    """A blurred copy of the caption in its own colour -- the halo drawn under it.

    `placed` is [((cx, cy), line), ...] with the same centres the caption is drawn
    at, so the glow sits exactly behind the text rather than being offset like a
    shadow. Drawn on its own canvas and blurred whole: blurring each line
    separately would leave a seam where two lines overlap.
    """
    glow = Image.new("RGBA", size, (0, 0, 0, 0))
    pen = ImageDraw.Draw(glow)
    spread = max(2, round(stroke * GLOW_SPREAD))
    for point, line in placed:
        pen.text(point, line, font=font, anchor="mm", fill=colour,
                 stroke_width=stroke + spread, stroke_fill=colour)
    glow = glow.filter(ImageFilter.GaussianBlur(spread))
    stacked = Image.new("RGBA", size, (0, 0, 0, 0))
    for _ in range(GLOW_PASSES):
        stacked.alpha_composite(glow)
    return stacked


def caption_window(item):
    """(start, end) a caption is on screen for, in the short's OWN timeline -- the
    jump-cut output the viewer sees, not the broadcast's clock. `end` None means it
    stays until the clip ends. A caption saved before timing existed has neither
    field and so covers the whole clip, which is what it used to do."""
    try:
        start = max(0.0, float(item.get("start") or 0.0))
    except (TypeError, ValueError):
        start = 0.0
    end = item.get("end")
    if end in (None, ""):
        return start, None
    try:
        end = float(end)
    except (TypeError, ValueError):
        return start, None
    return (start, None) if end <= start else (start, end)


def short_text_groups(texts):
    """[(start, end, [caption, ...]), ...] -- captions bucketed by the window they
    share, in first-appearance order. One PNG layer per bucket rather than per
    caption keeps the filter graph small: captions that appear together cost a
    single overlay between them."""
    groups = {}
    for item in texts or []:
        groups.setdefault(caption_window(item), []).append(item)
    return [(start, end, items) for (start, end), items in groups.items()]


def furniture_layer(size, brand=None, brand_rect=None, nameplates=None):
    """A full-frame RGBA image holding the channel logo and the player nameplates.

    Composited over the broadcast as a single ffmpeg input. Drawing it in PIL rather
    than with ffmpeg's `drawtext` keeps the typography under the same control as the
    board itself and sidesteps drawtext's filter-string escaping, which on Windows has
    to survive a font path containing both a drive colon and backslashes.

    `nameplates` is [(text, (x, y, w, h)), ...]; `brand` a path to a logo image, drawn
    into `brand_rect` with its aspect ratio preserved and centred.
    """
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)

    if brand and brand_rect:
        x, y, w, h = brand_rect
        art = Image.open(brand).convert("RGBA")
        scale = min(w / art.width, h / art.height)
        art = art.resize((max(1, int(art.width * scale)), max(1, int(art.height * scale))),
                         Image.LANCZOS)
        layer.alpha_composite(art, (int(x + (w - art.width) / 2), int(y + (h - art.height) / 2)))

    font_path = find_text_font()
    plate = (0xe9, 0x88, 0x39)
    for text, (x, y, w, h) in (nameplates or []):
        if not text:
            continue
        pad = max(4, int(h * 0.16))
        radius = max(3, int(h * 0.18))
        draw.rounded_rectangle((x, y, x + w, y + h), radius=radius, fill=(*plate, 255))
        font = boxed_font(draw, text, w - 2 * pad, h - 2 * pad, font_path)
        draw.text((x + w / 2, y + h / 2), text, font=font, anchor="mm", fill="#ffffff")
    return layer


def format_clock(seconds):
    """H:MM:SS once an hour is on the clock, M:SS below it -- the way a broadcast clock
    reads. Seconds are truncated rather than rounded: a clock reading 0:01 has not run
    out yet, and rounding 1.6 up to 0:02 would show time the player never had."""
    if seconds is None:
        return "--:--"
    total = max(0, int(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def _blend(colour, towards, amount):
    """`colour` mixed `amount` of the way towards `towards`; either may be a hex string."""
    a, b = ImageColor.getrgb(colour), ImageColor.getrgb(towards)
    return tuple(round(x + (y - x) * amount) for x, y in zip(a, b))


def draw_clocks(draw, box, clocks, to_move, theme=DEFAULT_THEME, orientation="white"):
    """Two plates under the board holding each side's remaining time.

    The plates carry the piece colours -- pale for White, dark for Black -- so nothing
    has to label whose clock is whose, and the side to move keeps full contrast while
    the idle one fades towards the page, which is what makes the running clock obvious
    at a glance. The board's near side takes the left plate.
    """
    x0, y0, x1, y1 = box
    height = y1 - y0
    _, _, _, accent, background = theme_colours(theme)
    split = max(4, int(height * 0.22))
    plate_w = (x1 - x0 - split) // 2
    radius = max(3, int(height * 0.24))
    pad = max(4, int(height * 0.18))
    font_path = find_text_font()
    near = orientation if orientation in ("white", "black") else "white"
    for index, side in enumerate((near, "black" if near == "white" else "white")):
        left = x0 + index * (plate_w + split)
        fill, ink = ("#f2f2ef", "#26262b") if side == "white" else ("#26262b", "#f2f2ef")
        if side != to_move:
            fill, ink = _blend(fill, background, 0.5), _blend(ink, background, 0.35)
        draw.rounded_rectangle((left, y0, left + plate_w, y1), radius=radius, fill=fill,
                               outline=accent if side == to_move else None,
                               width=max(2, height // 14))
        text = format_clock(clocks[0 if side == "white" else 1])
        font = boxed_font(draw, text, plate_w - 2 * pad, height - 2 * pad, font_path)
        draw.text((left + plate_w / 2, (y0 + y1) / 2), text, font=font, anchor="mm", fill=ink)


def draw_eval_bar(draw, box, share, text="", near="white"):
    """Vertical advantage bar: the side at the board's near end grows from the bottom.

    `share` is that near side's share of the bar, so a flipped board hands in Black's
    share and `near="black"` -- both the proportion and the two fills have to turn
    over together, or the bottom stays painted white while it measures Black.
    """
    x0, y0, x1, y1 = box
    width, height = x1 - x0, y1 - y0
    radius = max(2, width // 3)
    near_fill, far_fill = ("#f2f2ef", "#26262b") if near == "white" else ("#26262b", "#f2f2ef")
    draw.rounded_rectangle(box, radius=radius, fill=far_fill)
    split = y1 - int(height * max(0.0, min(1.0, share)))
    if split < y1 - 1:
        draw.rounded_rectangle((x0, max(y0, split - radius), x1, y1),
                               radius=radius, fill=near_fill)
    if split > y0 + 1:
        draw.rounded_rectangle((x0, y0, x1, min(y1, split + radius)),
                               radius=radius, fill=far_fill)
    draw.line((x0, split, x1, split), fill="#9a9a92", width=max(1, width // 12))
    if not text:
        return
    font = fitted_font(draw, text, width * 0.92, max(24, int(width * 0.85)))
    leading = share >= 0.5                    # the side that is winning holds the label
    pad = max(2, int(width * 0.22))
    # the label sits inside the leading side's fill, so it takes the other one as ink
    draw.text(((x0 + x1) / 2, y1 - pad if leading else y0 + pad), text, font=font,
              fill=far_fill if leading else near_fill,
              anchor="ms" if leading else "ma")


# core.classify's grades, in chess.com's own colours -- the vocabulary the audience
# already reads. "excellent" and "good" are deliberately absent: between them they
# cover most of a strong game, and badging those would leave a marker on nearly every
# move, which is the opposite of making the two that matter stand out.
BADGES = {
    "brilliant":  ("!!", "#26c2a3"),
    "great":      ("!",  "#5c8bb0"),
    "best":       ("★",  "#96bc4b"),
    "inaccuracy": ("?!", "#f7c631"),
    "mistake":    ("?",  "#ffa459"),
    "blunder":    ("??", "#fa412d"),
}
BADGE_SIZE = 0.54          # of one square


# Extra weight painted around the glyph, as a fraction of the badge. The symbols
# carry the whole meaning at maybe 30 px in a 1080p frame, so they are stroked in
# their own fill colour rather than left at the face's natural weight. Kept low on
# purpose: measured at 0.024 and above the stroke closes the gap under an exclamation
# mark, and "!" stops reading as a mark at all -- it turns into a plain white bar.
BADGE_STROKE = 0.016
# Air between the two marks of "!!", "??" and "?!". Stroked glyphs grow towards each
# other from both sides, and at badge size the pair fused into one blob without it.
BADGE_TRACKING = 0.10


def badge_font(draw, symbol, box_w, box_h):
    """The face to draw one badge symbol in, already sized to the box.

    Two faces, because neither covers all six grades: find_text_font() is the bold
    one the punctuation grades want, but Segoe UI Bold has no star glyph at all --
    "best" came out as an empty tofu box -- and find_font(), the symbol face that
    does carry the star, ships in one regular weight only. So the choice follows the
    symbol: plain punctuation gets the bold face, anything else the symbol face.
    """
    path = find_text_font() if symbol.isascii() else find_font()
    return boxed_font(draw, symbol, box_w, box_h, path)


def draw_badge(draw, square_box, board_box, label):
    """A grade marker on the corner of the square the move landed on.

    Sat on the top-right corner rather than over the middle of the square, so it
    never hides the piece it is talking about; pulled back inside the board when the
    move landed on an edge square, where a corner-centred badge would hang off into
    the margin."""
    symbol, colour = BADGES[label]
    x0, y0, x1, y1 = square_box
    size = (x1 - x0) * BADGE_SIZE
    half = size / 2
    cx = min(max(x1, board_box[0] + half), board_box[2] - half)
    cy = min(max(y0, board_box[1] + half), board_box[3] - half)
    draw.ellipse((cx - half, cy - half, cx + half, cy + half), fill=colour,
                 outline="#ffffff", width=max(1, int(size * 0.07)))
    stroke = max(1, round(size * BADGE_STROKE))
    tracking = size * BADGE_TRACKING if len(symbol) > 1 else 0.0
    # the stroke grows the glyph outwards and the tracking pushes the marks apart, so
    # both come off the box the face is fitted into rather than out of the badge edge
    font = badge_font(draw, symbol,
                      size * 0.66 - 2 * stroke - tracking * (len(symbol) - 1),
                      size * 0.62 - 2 * stroke)
    widths = [draw.textlength(mark, font=font) for mark in symbol]
    x = cx - (sum(widths) + tracking * (len(symbol) - 1)) / 2
    for mark, width in zip(symbol, widths):
        draw.text((x + width / 2, cy), mark, font=font, anchor="mm", fill="#ffffff",
                  stroke_width=stroke, stroke_fill="#ffffff")
        x += width + tracking


def board_image(timeline, ply, size=(1920, 1080), orientation="white",
                theme=DEFAULT_THEME, coordinates=True, piece_set="cburnett",
                evaluation=None, progress=None, clocks=None, classification=None):
    """`progress` in [0, 1) mid-slides the last move's piece(s) from origin to
    destination instead of showing them landed; omit it for the settled position.
    `clocks` is the (white_seconds, black_seconds) pair left after this ply, drawn on
    the strip under the board; None leaves the clock display off entirely.
    `classification` is one of core.classify's grades for the move that just landed;
    it is drawn only once the piece has settled, so the badge pops in on arrival
    instead of gliding along with the piece."""
    width, height = size
    light, dark, hi_light, hi_dark, background = theme_colours(theme)
    image = Image.new("RGB", size, background)
    draw = ImageDraw.Draw(image)
    # the bar needs its own column, so size the board against the width left over
    base = int(min(height * 0.94, width * 0.94) // 8)
    bar_width = max(10, int(base * 0.42)) if evaluation is not None else 0
    gap = max(4, int(base * 0.18)) if evaluation is not None else 0
    # the clock strip eats into the height the way the eval bar eats into the width
    rows = 8 + (CLOCK_HEIGHT + CLOCK_GAP if clocks else 0)
    square = int(min(height * 0.94 / rows, (width - bar_width - gap) * 0.94 / 8))
    side = square * 8
    strip = int(square * (CLOCK_HEIGHT + CLOCK_GAP)) if clocks else 0
    ox = (width - side - bar_width - gap) // 2 + bar_width + gap
    oy = (height - side - strip) // 2

    board = pgn.board_from(timeline)
    last, castle = None, None
    for move in timeline["moves"][:ply]:
        last = chess.Move.from_uci(move["uci"])
        castle = castling_squares(board, last)
        board.push(last)

    # squares whose landed piece is drawn mid-flight instead, plus where it flies from/to
    movers, hidden = [], set()
    if progress is not None and progress < 1.0 and last is not None:
        landed = castle[0] if castle else last.to_square
        movers.append((board.piece_at(landed), last.from_square, landed))
        hidden.add(landed)
        if castle:
            _, rook_from, rook_to = castle
            movers.append((board.piece_at(rook_to), rook_from, rook_to))
            hidden.add(rook_to)

    ranks = range(7, -1, -1) if orientation == "white" else range(8)
    files = range(8) if orientation == "white" else range(7, -1, -1)

    try:
        from core import pieces
        artwork = pieces.available(piece_set)
    except Exception:
        artwork = False
    piece_font = None if artwork else ImageFont.truetype(str(find_font()), int(square * 0.78))
    label_font = ImageFont.truetype(str(find_font()), max(10, int(square * 0.16)))

    def draw_piece(piece, x, y):
        if artwork:
            art = pieces.piece_image(piece.symbol(), square, piece_set)
            image.paste(art, (int(x), int(y)), art)
        else:
            draw.text((x + square / 2, y + square / 2), GLYPHS[piece.symbol()],
                      font=piece_font, anchor="mm",
                      fill="#ffffff" if piece.color else "#111111",
                      stroke_width=max(1, square // 34),
                      stroke_fill="#111111" if piece.color else "#dddddd")

    for row, rank in enumerate(ranks):
        for col, file in enumerate(files):
            index = chess.square(file, rank)
            pale = (file + rank) % 2
            colour = light if pale else dark
            if last and index in (last.from_square, last.to_square):
                colour = hi_light if pale else hi_dark
            box = (ox + col * square, oy + row * square,
                   ox + (col + 1) * square, oy + (row + 1) * square)
            draw.rectangle(box, fill=colour)
            if coordinates:
                # coordinates sit inside the board edge, the way modern boards do it
                ink = dark if pale else light
                if col == 0:
                    draw.text((box[0] + square * 0.07, box[1] + square * 0.05),
                              str(rank + 1), font=label_font, fill=ink)
                if row == 7:
                    draw.text((box[2] - square * 0.07, box[3] - square * 0.05),
                              "abcdefgh"[file], font=label_font, fill=ink, anchor="rd")
            if index in hidden:
                continue
            piece = board.piece_at(index)
            if piece:
                draw_piece(piece, box[0], box[1])

    for piece, from_square, to_square in movers:
        if piece is None:
            continue
        x0, y0 = _square_pixel(from_square, ox, oy, square, orientation)
        x1, y1 = _square_pixel(to_square, ox, oy, square, orientation)
        draw_piece(piece, x0 + (x1 - x0) * progress, y0 + (y1 - y0) * progress)

    if classification in BADGES and last is not None and progress is None:
        landed = castle[0] if castle else last.to_square
        bx, by = _square_pixel(landed, ox, oy, square, orientation)
        draw_badge(draw, (bx, by, bx + square, by + square),
                   (ox, oy, ox + side, oy + side), classification)

    if evaluation is not None:
        from core.evaluation import advantage, label
        # the bar follows the board: whoever sits at the bottom grows from the bottom,
        # so a flipped board does not put Black's advantage at White's end
        share = advantage(evaluation)
        near = "black" if orientation == "black" else "white"
        draw_eval_bar(draw, (ox - gap - bar_width, oy, ox - gap, oy + side),
                      1.0 - share if near == "black" else share, label(evaluation), near)
    if clocks:
        top = oy + side + int(square * CLOCK_GAP)
        draw_clocks(draw, (ox, top, ox + side, top + int(square * CLOCK_HEIGHT)), clocks,
                    "white" if ply % 2 == 0 else "black", theme, orientation)
    return image


def pick_encoder():
    probe = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
         "-i", "color=size=640x360:rate=1", "-frames:v", "1", "-c:v", "h264_nvenc",
         "-f", "null", "-"], capture_output=True)
    return "h264_nvenc" if probe.returncode == 0 else "libx264"


def _run_ffmpeg(build_command, encoder, label):
    """Run an ffmpeg encode, falling back to libx264 if a hardware encoder fails.

    h264_nvenc can pass pick_encoder()'s single-frame probe yet still die silently
    partway through a long real encode (GPU driver TDR, VRAM exhaustion) -- with no
    stderr at all, just a non-zero exit code. Losing a 20-minute render to that is
    worse than the encode being a bit slower, so retry once on CPU before giving up.
    """
    result = subprocess.run(build_command(encoder), capture_output=True, text=True)
    if result.returncode and encoder != "libx264":
        log.warning("%s: %s failed (%s), retrying with libx264", label, encoder,
                    result.stderr.strip()[-300:] or "no error output from ffmpeg")
        encoder = "libx264"
        result = subprocess.run(build_command(encoder), capture_output=True, text=True)
    if result.returncode:
        detail = result.stderr.strip()[-800:] or "ffmpeg exited with no error output"
        raise RuntimeError(f"{label}: {detail}")
    return encoder


def durations_from_waypoints(waypoints, tail=3.0):
    """[(ply, seconds)] — ply n is on screen until ply n+1 lands."""
    times = [w["timestamp"] for w in waypoints if w["timestamp"] is not None]
    if not times:
        raise ValueError("no timestamps to render")
    plan = [(0, times[0])]
    for index in range(len(times) - 1):
        plan.append((index + 1, max(0.04, times[index + 1] - times[index])))
    plan.append((len(times), tail))
    return plan


def render(timeline, plan, output, size=(1920, 1080), fps=30, encoder=None,
           theme=DEFAULT_THEME, piece_set="cburnett", evaluations=None, transition=0.15,
           clocks=None, orientation="white", classifications=None):
    """`transition` is how long (seconds) a moved piece takes to slide into place;
    0 falls back to an instant cut, like the previous one-frame-per-ply behaviour.
    `clocks` is pgn.clock_series(): index n is the pair shown after n plies. The clock
    only changes when a move lands, so it stays per-ply like everything else here --
    no extra frames, no cost to the concat plan.
    `orientation` is which side sits at the bottom; "black" flips the board, and the
    eval bar and clock plates follow it.
    `classifications` is core.classify's per-ply labels, indexed like `evaluations`:
    a badge on the square each graded move landed on."""
    if not shutil.which("ffmpeg"):
        raise RuntimeError("FFmpeg not installed")
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    encoder = encoder or pick_encoder()
    with tempfile.TemporaryDirectory() as directory:
        directory = Path(directory)
        entries = []
        index = 0
        for ply, seconds in plan:
            score = None
            if evaluations is not None:
                score = evaluations[ply - 1] if 0 < ply <= len(evaluations) else {"cp": 0}
            clock = clocks[ply] if clocks and ply < len(clocks) else None
            grade = (classifications[ply - 1] if classifications and 0 < ply <= len(classifications)
                     else None)
            slide = min(transition, seconds) if ply > 0 else 0.0
            if slide > 0:
                steps = max(2, round(slide * fps))
                step_seconds = slide / steps
                frames = [(k / steps, step_seconds) for k in range(1, steps)]
                frames.append((None, step_seconds + (seconds - slide)))
            else:
                frames = [(None, seconds)]
            for progress, duration in frames:
                frame = directory / f"{index:05d}.png"
                board_image(timeline, ply, size, orientation=orientation, theme=theme,
                            piece_set=piece_set, evaluation=score, progress=progress,
                            clocks=clock, classification=grade).save(frame)
                entries.append(f"file '{frame.as_posix()}'\nduration {duration:.4f}")
                index += 1
        entries.append(f"file '{(directory / f'{index - 1:05d}.png').as_posix()}'")
        listing = directory / "frames.txt"
        listing.write_text("\n".join(entries) + "\n", encoding="utf-8")
        def build_command(enc):
            return ["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
                    "-i", str(listing), "-fps_mode", "cfr", "-r", str(fps),
                    "-c:v", enc, "-pix_fmt", "yuv420p", str(output)]
        _run_ffmpeg(build_command, encoder, "FFmpeg failed")
    return output


def zoom_crop_rect(size, zoom):
    """Pixel crop box (w, h, x, y) for a {"percent", "x", "y"} zoom spec against
    `size` (width, height) -- the frame's own resolution. Shared by overlay_composite
    and the layout-picker's frame preview, so both crop identically."""
    out_w, out_h = size
    factor = zoom["percent"] / 100
    crop_w, crop_h = round(out_w / factor), round(out_h / factor)
    cx, cy = zoom.get("x", 0.5) * out_w, zoom.get("y", 0.5) * out_h
    crop_x = round(min(max(cx - crop_w / 2, 0), out_w - crop_w))
    crop_y = round(min(max(cy - crop_h / 2, 0), out_h - crop_h))
    return crop_w, crop_h, crop_x, crop_y


def composite_stages(rect, logo_rects=None, blur_rects=None, furniture=None, zoom=None,
                     size=None, freeze=None):
    """The filter graph that turns the broadcast ([0:v]) plus a board ([1:v]) and an
    optional furniture PNG ([2:v]) into the finished frame, ending at [v]. Shared by
    overlay_composite (the render) and composite_frame (the step-5 preview), so a
    preview can never drift from what the render will produce. `freeze` holds the
    board's last frame for that many seconds (tpad) -- only meaningful for video."""
    x, y, width, height = rect
    zoom_active = bool(zoom and zoom.get("percent", 100) > 100)
    label = "0:v"
    stages = []
    if zoom_active:
        crop_w, crop_h, crop_x, crop_y = zoom_crop_rect(size, zoom)
        out_w, out_h = size
        stages.append(f"[{label}]crop={crop_w}:{crop_h}:{crop_x}:{crop_y},"
                      f"scale={out_w}:{out_h}[zoomed]")
        label = "zoomed"
    if logo_rects:
        chain = ",".join(f"delogo=x={lx}:y={ly}:w={lw}:h={lh}:show=0" for lx, ly, lw, lh in logo_rects)
        stages.append(f"[{label}]{chain}[clean]")
        label = "clean"
    for index, (bx, by, bw, bh) in enumerate(blur_rects or []):
        # ffmpeg cannot blur a sub-region in place: cut the patch out, blur it, put it back
        radius = max(2, min(bw, bh) // 6)
        stages.append(f"[{label}]split=2[keep{index}][cut{index}]")
        stages.append(f"[cut{index}]crop={bw}:{bh}:{bx}:{by},boxblur={radius}:2[soft{index}]")
        stages.append(f"[keep{index}][soft{index}]overlay={bx}:{by}[blur{index}]")
        label = f"blur{index}"
    # board.mp4 starts at broadcast second 0 and never outlasts `end` by design, so
    # freezing it for `end` seconds is always enough; -t cuts the excess.
    tpad = f",tpad=stop_mode=clone:stop_duration={freeze:g}" if freeze else ""
    stages.append(f"[1:v]scale={width}:{height}:force_original_aspect_ratio=decrease{tpad}[b]")
    board_out = "board" if furniture else "v"
    stages.append(f"[{label}][b]overlay="
                  f"{x}+({width}-w)/2:{y}+({height}-h)/2:shortest=1[{board_out}]")
    if furniture:
        stages.append(f"[{board_out}][2:v]overlay=0:0[v]")
    return stages


def composite_frame(source, board_png, t, rect, logo_rects=None, blur_rects=None,
                    furniture=None, zoom=None, size=None, width=960) -> bytes:
    """One finished frame at broadcast second `t` as JPEG bytes: the same graph as the
    render, with a still board image instead of board.mp4. Lets the render step show
    what the long video will look like before anything has been rendered."""
    stages = composite_stages(rect, logo_rects=logo_rects, blur_rects=blur_rects,
                              furniture=furniture, zoom=zoom, size=size)
    stages[-1] = stages[-1].removesuffix("[v]") + f",scale={width}:-2[v]"   # preview size
    inputs = ["-ss", f"{max(0.0, t):.3f}", "-i", str(source), "-i", str(board_png)]
    if furniture:
        inputs += ["-i", str(furniture)]
    out = subprocess.run(["ffmpeg", "-v", "error", *inputs, "-filter_complex", ";".join(stages),
                          "-map", "[v]", "-frames:v", "1", "-f", "image2", "-c:v", "mjpeg", "-q:v", "4", "-"],
                         capture_output=True)
    if out.returncode or not out.stdout:
        raise RuntimeError(f"Preview frame failed: {out.stderr.decode(errors='replace')[-300:]}")
    return out.stdout


def overlay_composite(source, board_video, output, rect, logo_rects=None, encoder=None,
                      blur_rects=None, furniture=None, zoom=None,
                      size=None, start=0.0, end=None):
    """Paste the generated board over the broadcast, in the spot its digital overlay
    occupies, so the rendered board replaces the overlay in the original footage.

    `rect` is (x, y, w, h) in source-video pixels: either the detected overlay square
    or a box the user drew. The board keeps its own aspect ratio and is centred in the
    box -- with the eval bar on, board.mp4 is wider than it is tall, so stretching it
    to a square box would visibly squash the pieces.
    `logo_rects` is a list of (x, y, w, h) boxes to blot out via ffmpeg's `delogo`
    (interpolates each box from its surrounding pixels — no AI); `blur_rects` the same
    but blurred, for areas that keep changing and so smear under interpolation.
    `furniture`, if given, is a full-frame RGBA PNG (see furniture_layer) laid on last,
    so the channel logo and nameplates sit above everything else.
    `end` is the broadcast second the video stops at (the user's "selesai" box: the
    last move plus however long they want to keep). board.mp4 runs out a few seconds
    after the last move while the stream keeps rolling, so its final position is
    frozen (tpad clone) for as long as `end` needs -- the board vanishing would read
    as the video breaking. None stops when board.mp4 does. Either way it is capped
    by the broadcast's own length, since the overlay ends with the shorter input.
    The broadcast's own audio is dropped; only board_video's track (the move clicks,
    if enabled) survives, since board_video has no audio stream at all when they're off.
    `zoom`, if given, is {"percent": >=100, "x": 0-1, "y": 0-1} -- a crop centred at the
    normalised (x, y) point, sized to `percent` of the frame and scaled back up to
    `size` (required together with `zoom`). Applied FIRST, before delogo/blur/board/
    nameplates, so every other box (`rect`, `logo_rects`, ...) is defined against this
    already-zoomed frame -- matching the layout picker, which previews this same crop
    (see zoom_crop_rect) so what the user draws on lines up with what actually renders.
    `start` drops everything before that second of the broadcast -- the pre-game
    build-up, which is dead air in the finished video. board.mp4 is generated on the
    broadcast's own clock (plan[0] holds the starting position until the first move
    lands), so the SAME seek is applied to both inputs and they stay in step; trimming
    here rather than in the plan leaves board.mp4 itself untouched, so the short --
    which reuses that file and counts from its start -- is unaffected.
    """
    encoder = encoder or pick_encoder()
    stages = composite_stages(rect, logo_rects=logo_rects, blur_rects=blur_rects,
                              furniture=furniture, zoom=zoom, size=size, freeze=end)

    def build_command(enc):
        quality = ["-cq", "23"] if enc == "h264_nvenc" else ["-crf", "20", "-preset", "veryfast"]
        # input seeking (before -i) so both decoders skip the dead air instead of
        # decoding and discarding it; each input's timestamps then restart at 0.
        # Floored onto the frame grid, then nudged a millisecond below it. A seek
        # landing mid-frame -- or a hair ABOVE a frame's timestamp, which is what
        # floor() alone produces once the division is written out in decimal --
        # makes ffmpeg start the video one frame late while the audio still starts
        # at 0, so the move clicks run a frame ahead of the picture. Measured: -ss
        # 5.233333 gives a 0.033s video start_time, 5.233000 gives 0.000. Flooring
        # and the nudge only ever keep a few extra milliseconds, never clip the move.
        snapped = max(0.0, math.floor(start * BOARD_FPS) / BOARD_FPS - 0.001)
        seek = ["-ss", f"{snapped:.4f}"] if snapped > 0 else []
        inputs = [*seek, "-i", str(source), *seek, "-i", str(board_video)]
        if furniture:
            inputs += ["-i", str(furniture)]
        return ["ffmpeg", "-y", "-loglevel", "error", *inputs,
                "-filter_complex", ";".join(stages),
                "-map", "[v]", "-map", "1:a?", "-c:v", enc, *quality, "-pix_fmt", "yuv420p",
                *(["-t", f"{end - snapped:.3f}"] if end else []),
                "-c:a", "aac", "-b:a", "192k", str(output)]
    _run_ffmpeg(build_command, encoder, "Full-video render failed")
    return Path(output)


# short_clip layout: fractions of the 1080x1920 canvas, used only when the user
# hasn't dragged a custom board_rect
SHORT_SIZE = (1080, 1920)
SHORT_BOARD_WIDTH = 0.70    # default board box width -- narrower than the full canvas on purpose
SHORT_TEXT_GAP = 0.09       # blank strip under the source video, left for a caption
SHORT_BOTTOM_MARGIN = 0.04  # blank strip under the board
SHORT_BLUR_SIGMA = 40       # backdrop blur strength, at SHORT_SIZE's own resolution
SHORT_BLUR_DARKEN = 0.08    # backdrop brightness cut, so yellow caption text still pops
SHORT_PAD = 1.0             # default seconds kept before/after each included ply lands
SHORT_TAIL = 0.0            # default extra seconds held after the last included ply


def short_top_height(width, src_w, src_h):
    """Height the source video occupies at the top of the short canvas, letterboxed
    to the full canvas width with its own aspect ratio kept exactly (no crop)."""
    return round(width * src_h / src_w / 2) * 2


def default_short_board_rect(size, top_h):
    """Where the board sits when the user hasn't dragged a custom box: centred,
    SHORT_BOARD_WIDTH of the canvas, filling the space below the video down to
    SHORT_BOTTOM_MARGIN from the bottom."""
    width, height = size
    gap = round(height * SHORT_TEXT_GAP)
    bottom_margin = round(height * SHORT_BOTTOM_MARGIN)
    board_y = top_h + gap
    board_w = round(width * SHORT_BOARD_WIDTH)
    board_h = max(10, height - board_y - bottom_margin)
    return (round((width - board_w) / 2), board_y, board_w, board_h)


def short_included_waypoints(waypoints, plies):
    """The last `plies` plies that actually have a timestamp, in ply order --
    the ones short_clip() builds a window around. `plies` <= 0 means all of them."""
    timed = [w for w in waypoints if w.get("timestamp") is not None]
    return timed[-plies:] if plies > 0 else timed


def short_windows(waypoints, plies, pad, tail=SHORT_TAIL, limit=None):
    """[(start, end), ...] -- one `2*pad`-second window per included ply, merged
    where they overlap, sorted by time. This is the whole trick behind keeping a
    short under any given length regardless of how long a player actually spent
    thinking: instead of playing the broadcast in real time between moves, only
    `pad` seconds either side of each move landing is kept, and every stretch of
    "nothing happening" in between is skipped outright.

    `tail` extends the last window past that final `pad`, so the closing position
    stays on screen instead of cutting the instant it lands. `limit`, when given,
    is the source's own duration -- nothing is kept past the end of the video, so
    a tail longer than what is left simply stops there."""
    included = short_included_waypoints(waypoints, plies)
    raw = sorted((max(0.0, w["timestamp"] - pad), w["timestamp"] + pad) for w in included)
    merged = []
    for start, end in raw:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    if merged and tail > 0:
        merged[-1] = (merged[-1][0], merged[-1][1] + tail)
    if limit is not None:
        merged = [(start, min(end, limit)) for start, end in merged if start < limit]
    return merged


def short_total_duration(waypoints, plies, pad, tail=SHORT_TAIL, limit=None):
    return sum(end - start for start, end in short_windows(waypoints, plies, pad, tail, limit))


def short_reference_time(waypoints, plies):
    """A representative timestamp for the short -- the earliest included ply's
    landing time. Only used to grab a single preview frame; the real render pulls
    many separate windows (see short_windows)."""
    included = short_included_waypoints(waypoints, plies)
    return included[0]["timestamp"] if included else 0.0


def short_clip(source, board_video, output, waypoints, plies, board_rect=None, text_layers=None,
               pad=SHORT_PAD, tail=SHORT_TAIL, size=SHORT_SIZE, encoder=None):
    """A vertical highlight clip: a `pad`-second window around each of the last
    `plies` plies, jump-cut together -- see short_windows() -- so the result stays
    short no matter how long any single move took to think through in the original
    broadcast. board_video shares source's time axis (both start at the game's
    t=0), so the same windows apply to both without any separate lookup.

    `tail` holds the closing position for extra seconds after the last ply lands.
    board.mp4 already carries a few seconds past its own final ply (see
    durations_from_waypoints), and past that the board overlay simply freezes on
    its last frame while the source keeps running -- either way the final position
    stays on screen for the whole tail.

    Layout: the source video sits flush against the top edge at its full frame (no
    crop, letterboxed to the canvas width so nothing is cut off). `board_rect`
    ([x, y, w, h] in canvas pixels), if given, overrides where the board goes --
    its own aspect ratio is kept and it's centred inside that box, same convention
    as `paste_rect` in overlay_composite; None falls back to default_short_board_rect.
    `text_layers` is [(png_path, start, end), ...]: full-canvas RGBA captions (see
    short_text_layer) composited last, each only on screen between `start` and `end`
    seconds of the *output* clip -- overlay's `enable` reads the timeline after the
    jump cut, which is the same clock the user sets those seconds against. `end`
    None keeps a layer up to the end.
    """
    from core.video import has_audio
    encoder = encoder or pick_encoder()
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    src_info = probe(source)
    windows = short_windows(waypoints, plies, pad, tail, limit=src_info.get("duration"))
    if not windows:
        raise ValueError("No timed plies to build a short from")
    select_expr = "+".join(f"between(t,{start:.3f},{end:.3f})" for start, end in windows)

    width, height = size
    top_h = short_top_height(width, src_info["width"], src_info["height"])
    bx, by, bw, bh = (int(round(v)) for v in (board_rect or default_short_board_rect(size, top_h)))
    board_has_audio = has_audio(board_video)

    layers = list(text_layers or [])

    def build_command(enc):
        quality = ["-cq", "23"] if enc == "h264_nvenc" else ["-crf", "20", "-preset", "veryfast"]
        board_label = "c2" if layers else "v"
        # backdrop: the same jump-cut source frames, scaled to cover the whole
        # canvas and heavily blurred, filling what would otherwise be flat black
        # behind the gap/margins -- the same "blurred fill" look as the reference
        stages = [
            f"[0:v]select='{select_expr}',setpts=N/FRAME_RATE/TB,split=2[srcbg][srctop]",
            f"[1:v]select='{select_expr}',setpts=N/FRAME_RATE/TB[brd]",
            f"[srcbg]scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},gblur=sigma={SHORT_BLUR_SIGMA},"
            f"eq=brightness=-{SHORT_BLUR_DARKEN}[canvas]",
            f"[srctop]scale={width}:{top_h}[top]",
            f"[brd]scale={bw}:{bh}:force_original_aspect_ratio=decrease[boardv]",
            "[canvas][top]overlay=0:0:shortest=1[c1]",
            f"[c1][boardv]overlay={bx}+({bw}-w)/2:{by}+({bh}-h)/2[{board_label}]",
        ]
        inputs = ["-i", str(source), "-i", str(board_video)]
        maps = ["-map", "[v]"]
        if board_has_audio:
            stages.append(f"[1:a]aselect='{select_expr}',asetpts=N/SR/TB[a]")
            maps += ["-map", "[a]"]
        # one overlay per caption group, chained; a group covering the whole clip
        # needs no `enable` at all, so an untimed short builds the same graph as before
        source_label = board_label
        for index, (layer_path, start, end) in enumerate(layers):
            inputs += ["-i", str(layer_path)]
            out_label = "v" if index == len(layers) - 1 else f"t{index}"
            enable = ""
            if start > 0 or end is not None:
                until = f"{end:.3f}" if end is not None else "1e9"
                enable = f":enable='between(t,{start:.3f},{until})'"
            stages.append(f"[{source_label}][{index + 2}:v]overlay=0:0{enable}[{out_label}]")
            source_label = out_label
        return ["ffmpeg", "-y", "-loglevel", "error", *inputs,
                "-filter_complex", ";".join(stages), *maps,
                "-c:v", enc, *quality, "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "192k", str(output)]
    _run_ffmpeg(build_command, encoder, "Short render failed")
    return output
