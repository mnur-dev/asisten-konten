import json
from pathlib import Path
from PIL import Image
from shared.timeline import parse_pgn
from renderer.render import render_frame, render_video

ROOT = Path(__file__).parents[1]
PGN = ROOT / "samples/grischuk-wei.pgn"

def test_parse_sample_has_92_plies():
    timeline = parse_pgn(PGN.read_text(encoding="utf-8"))
    assert len(timeline["moves"]) == 92
    assert timeline["moves"][0]["san"] == "e4"
    assert timeline["moves"][-1]["san"] == "Rg1+"

def test_frame_is_png_with_requested_size(tmp_path):
    timeline = parse_pgn(PGN.read_text(encoding="utf-8"))
    output = tmp_path / "frame.png"
    render_frame(timeline, ply=1, output=output, size=(640, 360))
    with Image.open(output) as image:
        assert image.format == "PNG"
        assert image.size == (640, 360)

def test_short_video_is_created(tmp_path):
    timeline = parse_pgn(PGN.read_text(encoding="utf-8"))
    output = tmp_path / "short.mp4"
    render_video(timeline, output, size=(640, 360), fps=5, seconds_per_ply=.2, max_plies=5, encoder="libx264")
    assert output.stat().st_size > 1000
