import shutil
import subprocess
from pathlib import Path
import chess
from PIL import Image, ImageDraw, ImageFont

PIECES = {"P":"♙","N":"♘","B":"♗","R":"♖","Q":"♕","K":"♔","p":"♟","n":"♞","b":"♝","r":"♜","q":"♛","k":"♚"}

def find_chess_font():
    candidates = []
    if windir := __import__("os").environ.get("WINDIR"):
        candidates.append(Path(windir) / "Fonts" / "seguisym.ttf")
    candidates.extend((
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("/System/Library/Fonts/Apple Symbols.ttf"),
    ))
    for font in candidates:
        if font.is_file():
            return font
    raise RuntimeError("Chess Unicode font not found")

def _image(timeline, ply, size, orientation="white"):
    width, height = size
    image = Image.new("RGB", size, "#17191f")
    draw = ImageDraw.Draw(image)
    side = min(height, width * 3 // 4)
    square = side // 8
    side = square * 8
    ox, oy = (width - side) // 2, (height - side) // 2
    board = chess.Board(timeline["initial_fen"])
    last = None
    for item in timeline["moves"][:ply]:
        last = chess.Move.from_uci(item["uci"]); board.push(last)
    ranks = range(7,-1,-1) if orientation == "white" else range(8)
    files = range(8) if orientation == "white" else range(7,-1,-1)
    font = ImageFont.truetype(find_chess_font(), max(14, int(square * .72)))
    for row, rank in enumerate(ranks):
        for col, file in enumerate(files):
            sq = chess.square(file, rank)
            color = "#f0d9b5" if (file + rank) % 2 else "#b58863"
            if last and sq in (last.from_square, last.to_square): color = "#d7d75f"
            box=(ox+col*square,oy+row*square,ox+(col+1)*square,oy+(row+1)*square)
            draw.rectangle(box, fill=color)
            piece=board.piece_at(sq)
            if piece:
                glyph=PIECES[piece.symbol()]
                fill="#fafafa" if piece.color else "#181818"
                stroke="#181818" if piece.color else "#eeeeee"
                draw.text((box[0]+square/2,box[1]+square/2),glyph,font=font,fill=fill,stroke_width=max(1,square//40),stroke_fill=stroke,anchor="mm")
    return image

def render_frame(timeline, ply, output, size=(1920,1080), orientation="white"):
    output=Path(output); output.parent.mkdir(parents=True,exist_ok=True)
    _image(timeline,ply,size,orientation).save(output,"PNG")

def render_video(timeline, output, size=(1920,1080), fps=30, seconds_per_ply=1.0, max_plies=None, encoder=None):
    if not shutil.which("ffmpeg"): raise RuntimeError("FFmpeg not installed")
    if encoder is None:
        probe = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "color=size=640x360:rate=1", "-frames:v", "1", "-c:v", "h264_nvenc", "-f", "null", "-"],
            capture_output=True,
        )
        encoder = "h264_nvenc" if probe.returncode == 0 else "libx264"
    count=min(len(timeline["moves"]),max_plies or len(timeline["moves"]))
    cmd=["ffmpeg","-y","-loglevel","error","-f","rawvideo","-pix_fmt","rgb24","-s",f"{size[0]}x{size[1]}","-r",str(fps),"-i","-","-an","-c:v",encoder,"-pix_fmt","yuv420p",str(output)]
    proc=subprocess.Popen(cmd,stdin=subprocess.PIPE,stderr=subprocess.PIPE)
    frames=max(1,round(fps*seconds_per_ply))
    try:
        for ply in range(1,count+1):
            data=_image(timeline,ply,size).tobytes()
            for _ in range(frames): proc.stdin.write(data)
        proc.stdin.close(); error=proc.stderr.read().decode(); code=proc.wait()
    except Exception:
        proc.kill(); raise
    if code: raise RuntimeError(f"FFmpeg failed with {encoder}: {error.strip()}")
