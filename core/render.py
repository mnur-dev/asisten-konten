"""Render a board video whose ply durations follow detected timestamps.

One PNG per ply plus FFmpeg's concat demuxer, so encoding cost scales with the
number of moves rather than the number of output frames.
"""
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import chess
from PIL import Image, ImageDraw, ImageFont

GLYPHS = {"P": "♙", "N": "♘", "B": "♗", "R": "♖", "Q": "♕", "K": "♔",
          "p": "♟", "n": "♞", "b": "♝", "r": "♜", "q": "♛", "k": "♚"}

# light, dark, highlight-on-light, highlight-on-dark, page background
THEMES = {
    "orange":   ("#eeeed2", "#d68a3c", "#f6f669", "#f0b25b", "#262421"),
    "chesscom": ("#ebecd0", "#739552", "#f7f769", "#b9ca43", "#302e2b"),
    "blue":     ("#dee3e6", "#8ca2ad", "#cdd26a", "#aaa23b", "#22272b"),
    "wood":     ("#f0d9b5", "#b58863", "#f7ec74", "#dac431", "#2b2622"),
    "slate":    ("#e6e9ee", "#4a5568", "#e9c46a", "#b98a2f", "#171a20"),
}
DEFAULT_THEME = "orange"


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
        command = ["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
                   "-i", str(listing), "-fps_mode", "cfr", "-r", str(fps),
                   "-c:v", encoder, "-pix_fmt", "yuv420p", str(output)]
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode:
            raise RuntimeError(f"FFmpeg failed with {encoder}: {result.stderr.strip()[-800:]}")
    return output


def overlay_composite(source, board_video, output, rect, logo_rects=None, encoder=None):
    """Paste the generated board over the broadcast, in the spot its digital overlay
    occupies, so the rendered board replaces the overlay in the original footage.

    `rect` is (x, y, side) in source-video pixels, as detected by core.overlay.
    `logo_rects`, if given, is a list of (x, y, w, h) boxes to blot out first, via
    ffmpeg's `delogo` (interpolates each box from its surrounding pixels — no AI).
    The broadcast's own audio is dropped; only board_video's track (the move clicks,
    if enabled) survives, since board_video has no audio stream at all when they're off.
    """
    encoder = encoder or pick_encoder()
    quality = ["-cq", "23"] if encoder == "h264_nvenc" else ["-crf", "20", "-preset", "veryfast"]
    x, y, side = rect
    source_label = "0:v"
    stages = []
    if logo_rects:
        chain = ",".join(f"delogo=x={lx}:y={ly}:w={lw}:h={lh}:show=0" for lx, ly, lw, lh in logo_rects)
        stages.append(f"[0:v]{chain}[clean]")
        source_label = "clean"
    stages.append(f"[1:v]scale={side}:{side}[b]")
    stages.append(f"[{source_label}][b]overlay={x}:{y}:shortest=1[v]")
    command = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(source), "-i", str(board_video),
               "-filter_complex", ";".join(stages),
               "-map", "[v]", "-map", "1:a?", "-c:v", encoder, *quality, "-pix_fmt", "yuv420p",
               "-c:a", "aac", "-b:a", "192k", str(output)]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(f"Full-video render failed: {result.stderr.strip()[-800:]}")
    return Path(output)
