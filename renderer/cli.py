import argparse
from pathlib import Path
from shared.timeline import parse_pgn
from renderer.render import render_video

p=argparse.ArgumentParser()
p.add_argument("pgn"); p.add_argument("output"); p.add_argument("--width",type=int,default=1920); p.add_argument("--height",type=int,default=1080); p.add_argument("--fps",type=int,default=30); p.add_argument("--seconds-per-ply",type=float,default=1); p.add_argument("--max-plies",type=int)
a=p.parse_args(); render_video(parse_pgn(Path(a.pgn).read_text(encoding="utf-8")),a.output,(a.width,a.height),a.fps,a.seconds_per_ply,a.max_plies)
