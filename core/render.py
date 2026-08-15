"""Render a board video whose ply durations follow detected timestamps.

One PNG per ply plus FFmpeg's concat demuxer, so encoding cost scales with the
number of moves rather than the number of output frames.
"""
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import chess
from PIL import Image, ImageDraw, ImageFont

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
HOLD_AFTER_BOARD = 60.0


def theme_colours(name):
    return THEMES.get(name or DEFAULT_THEME, THEMES[DEFAULT_THEME])


# king (from, to) -> rook (from, to), for animating the rook alongside the king
_CASTLING_ROOKS = {
    (chess.E1, chess.G1): (chess.H1, chess.F1),
    (chess.E1, chess.C1): (chess.A1, chess.D1),
    (chess.E8, chess.G8): (chess.H8, chess.F8),
    (chess.E8, chess.C8): (chess.A8, chess.D8),
}


def _square_pixel(index, ox, oy, square, orientation):
    """Top-left pixel of a square's box, matching board_image's own layout."""
    file, rank = chess.square_file(index), chess.square_rank(index)
    col, row = (file, 7 - rank) if orientation == "white" else (7 - file, rank)
    return ox + col * square, oy + row * square


def fit_size(square_px=135, evaluation=False, margin=0.06):
    """Canvas sized tightly around the board (plus the eval bar strip), no letterboxing."""
    bar_width = max(10, int(square_px * 0.42)) if evaluation else 0
    gap = max(4, int(square_px * 0.18)) if evaluation else 0
    side = square_px * 8
    width = int((side + bar_width + gap) / (1 - margin))
    height = int(side / (1 - margin))
    return width, height


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


def furniture_layer(size, brand=None, brand_rect=None, nameplates=None,
                    theme=DEFAULT_THEME, opacity=0.72):
    """A full-frame RGBA image holding the channel logo and the player nameplates.

    Composited over the broadcast as a single ffmpeg input. Drawing it in PIL rather
    than with ffmpeg's `drawtext` keeps the typography under the same control as the
    board itself and sidesteps drawtext's filter-string escaping, which on Windows has
    to survive a font path containing both a drive colon and backslashes.

    `nameplates` is [(text, (x, y, w, h)), ...]; `brand` a path to a logo image, drawn
    into `brand_rect` with its aspect ratio preserved and centred.
    """
    light, _, _, _, background = theme_colours(theme)
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
    plate = tuple(int(c, 16) for c in (background[1:3], background[3:5], background[5:7]))
    for text, (x, y, w, h) in (nameplates or []):
        if not text:
            continue
        pad = max(4, int(h * 0.16))
        radius = max(3, int(h * 0.18))
        draw.rounded_rectangle((x, y, x + w, y + h), radius=radius,
                               fill=(*plate, int(255 * opacity)))
        font = boxed_font(draw, text, w - 2 * pad, h - 2 * pad, font_path)
        draw.text((x + w / 2, y + h / 2), text, font=font, anchor="mm", fill=light)
    return layer


def draw_eval_bar(draw, box, share, text=""):
    """Vertical advantage bar: white grows from the bottom, black from the top."""
    x0, y0, x1, y1 = box
    width, height = x1 - x0, y1 - y0
    radius = max(2, width // 3)
    draw.rounded_rectangle(box, radius=radius, fill="#26262b")
    split = y1 - int(height * max(0.0, min(1.0, share)))
    if split < y1 - 1:
        draw.rounded_rectangle((x0, max(y0, split - radius), x1, y1),
                               radius=radius, fill="#f2f2ef")
    if split > y0 + 1:
        draw.rounded_rectangle((x0, y0, x1, min(y1, split + radius)),
                               radius=radius, fill="#26262b")
    draw.line((x0, split, x1, split), fill="#9a9a92", width=max(1, width // 12))
    if not text:
        return
    font = fitted_font(draw, text, width * 0.92, max(24, int(width * 0.85)))
    leading = share >= 0.5                    # the side that is winning holds the label
    pad = max(2, int(width * 0.22))
    draw.text(((x0 + x1) / 2, y1 - pad if leading else y0 + pad), text, font=font,
              fill="#26262b" if leading else "#f2f2ef",
              anchor="ms" if leading else "ma")


def board_image(timeline, ply, size=(1920, 1080), orientation="white",
                theme=DEFAULT_THEME, coordinates=True, piece_set="cburnett",
                evaluation=None, progress=None):
    """`progress` in [0, 1) mid-slides the last move's piece(s) from origin to
    destination instead of showing them landed; omit it for the settled position."""
    width, height = size
    light, dark, hi_light, hi_dark, background = theme_colours(theme)
    image = Image.new("RGB", size, background)
    draw = ImageDraw.Draw(image)
    # the bar needs its own column, so size the board against the width left over
    base = int(min(height * 0.94, width * 0.94) // 8)
    bar_width = max(10, int(base * 0.42)) if evaluation is not None else 0
    gap = max(4, int(base * 0.18)) if evaluation is not None else 0
    square = int(min(height * 0.94, (width - bar_width - gap) * 0.94) // 8)
    side = square * 8
    ox = (width - side - bar_width - gap) // 2 + bar_width + gap
    oy = (height - side) // 2

    board = chess.Board(timeline["initial_fen"])
    last = None
    for move in timeline["moves"][:ply]:
        last = chess.Move.from_uci(move["uci"])
        board.push(last)

    # squares whose landed piece is drawn mid-flight instead, plus where it flies from/to
    movers, hidden = [], set()
    if progress is not None and progress < 1.0 and last is not None:
        movers.append((board.piece_at(last.to_square), last.from_square, last.to_square))
        hidden.add(last.to_square)
        if rook_squares := _CASTLING_ROOKS.get((last.from_square, last.to_square)):
            rook_from, rook_to = rook_squares
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

    if evaluation is not None:
        from core.evaluation import advantage, label
        draw_eval_bar(draw, (ox - gap - bar_width, oy, ox - gap, oy + side),
                      advantage(evaluation), label(evaluation))
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
           theme=DEFAULT_THEME, piece_set="cburnett", evaluations=None, transition=0.15):
    """`transition` is how long (seconds) a moved piece takes to slide into place;
    0 falls back to an instant cut, like the previous one-frame-per-ply behaviour."""
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
                board_image(timeline, ply, size, theme=theme, piece_set=piece_set,
                            evaluation=score, progress=progress).save(frame)
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


def overlay_composite(source, board_video, output, rect, logo_rects=None, encoder=None,
                      blur_rects=None, furniture=None, hold=HOLD_AFTER_BOARD):
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
    `hold` keeps the board's final position on screen for that many extra seconds.
    The overlay ends with the shorter input, and board.mp4 runs out well before the
    broadcast does (the game finishes; the stream keeps rolling through the handshake
    and interview), so without this the composite would stop dead on the last move.
    Freezing the final position is what a viewer expects there -- the board vanishing
    would read as the video breaking. Capped by the broadcast's own length.
    The broadcast's own audio is dropped; only board_video's track (the move clicks,
    if enabled) survives, since board_video has no audio stream at all when they're off.
    """
    encoder = encoder or pick_encoder()
    x, y, width, height = rect
    label = "0:v"
    stages = []
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
    freeze = f",tpad=stop_mode=clone:stop_duration={hold:g}" if hold else ""
    stages.append(f"[1:v]scale={width}:{height}:force_original_aspect_ratio=decrease{freeze}[b]")
    board_out = "board" if furniture else "v"
    stages.append(f"[{label}][b]overlay="
                  f"{x}+({width}-w)/2:{y}+({height}-h)/2:shortest=1[{board_out}]")
    if furniture:
        stages.append(f"[{board_out}][2:v]overlay=0:0[v]")

    def build_command(enc):
        quality = ["-cq", "23"] if enc == "h264_nvenc" else ["-crf", "20", "-preset", "veryfast"]
        inputs = ["-i", str(source), "-i", str(board_video)]
        if furniture:
            inputs += ["-i", str(furniture)]
        return ["ffmpeg", "-y", "-loglevel", "error", *inputs,
                "-filter_complex", ";".join(stages),
                "-map", "[v]", "-map", "1:a?", "-c:v", enc, *quality, "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "192k", str(output)]
    _run_ffmpeg(build_command, encoder, "Full-video render failed")
    return Path(output)
