"""Local web app: one FastAPI process on 127.0.0.1, no auth, no queue, no workers."""
import json
import logging
import shutil
import subprocess
import threading
import uuid
from pathlib import Path

from fastapi import Body, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response
from PIL import Image
from pydantic import BaseModel

from core import audio, evaluation, physical, pieces
from core.detect import detect
from core.pgn import parse_pgn
from core.render import (DEFAULT_THEME, THEMES, board_image, durations_from_waypoints,
                         fit_size, furniture_layer, overlay_composite, render)
from core.video import download, probe

ROOT = Path(__file__).parents[1]
PROJECTS = ROOT / "projects"
PROJECTS.mkdir(exist_ok=True)
UI = Path(__file__).parent / "ui" / "index.html"

app = FastAPI(title="Asisten Konten")
_running: dict[str, threading.Thread] = {}


def folder(project_id: str) -> Path:
    path = PROJECTS / project_id
    if not path.is_dir():
        raise HTTPException(404, "Project not found")
    return path


def read_meta(path: Path) -> dict:
    return json.loads((path / "meta.json").read_text(encoding="utf-8"))


def write_meta(path: Path, meta: dict) -> None:
    (path / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")


def update(path: Path, **fields) -> dict:
    meta = read_meta(path)
    meta.update(fields)
    write_meta(path, meta)
    return meta


def log_to(path: Path):
    handler = logging.FileHandler(path / "log.txt", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S"))
    logging.getLogger("core").addHandler(handler)
    logging.getLogger("core").setLevel(logging.INFO)
    return handler


def background(project_id: str, work):
    path = folder(project_id)

    def runner():
        handler = log_to(path)
        try:
            work(path)
        except Exception as error:                     # surfaced in the UI, not swallowed
            logging.getLogger("core").error("%s", error)
            update(path, status="failed", error=str(error))
        finally:
            logging.getLogger("core").removeHandler(handler)
            handler.close()
            _running.pop(project_id, None)

    if project_id in _running:
        raise HTTPException(409, "This project is already busy")
    thread = threading.Thread(target=runner, daemon=True)
    _running[project_id] = thread
    thread.start()


class NewProject(BaseModel):
    name: str = ""
    video_url: str
    pgn_text: str


@app.get("/", response_class=HTMLResponse)
def index():
    return UI.read_text(encoding="utf-8")


@app.get("/api/projects")
def list_projects():
    items = []
    for path in sorted(PROJECTS.iterdir(), reverse=True):
        if (path / "meta.json").is_file():
            meta = read_meta(path)
            items.append({"id": path.name, "name": meta.get("name"), "status": meta.get("status")})
    return {"projects": items}


@app.post("/api/projects", status_code=201)
def create(data: NewProject):
    url = data.video_url.strip()
    if not url:
        raise HTTPException(400, "Video link is required")
    try:
        timeline = parse_pgn(data.pgn_text)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error

    project_id = uuid.uuid4().hex[:12]
    path = PROJECTS / project_id
    path.mkdir()
    (path / "input.pgn").write_text(data.pgn_text, encoding="utf-8")
    video = path / "source.mp4"
    white, black = timeline["headers"].get("White", ""), timeline["headers"].get("Black", "")
    write_meta(path, {
        "id": project_id,
        "name": data.name or (f"{white} - {black}" if white or black else project_id),
        "video": str(video),
        "video_url": url,
        "status": "downloading",
        "plies_total": len(timeline["moves"]),
        "white": white,
        "black": black,
        "board_quad": None,
        "logo_rects": [],
        "theme": DEFAULT_THEME,
        "piece_set": pieces.BUNDLED,
        "sound": True,
        "eval_bar": False,
    })

    def work(path: Path):
        logging.getLogger("core").info("Mengunduh video dari %s", url)
        download(url, video)
        info = probe(video)
        logging.getLogger("core").info("Unduh video selesai")
        update(path, width=info["width"], height=info["height"], duration=round(info["duration"], 2))

        if scores := evaluation.from_pgn(data.pgn_text, len(timeline["moves"])):
            (path / "evals.json").write_text(json.dumps(scores), encoding="utf-8")
            logging.getLogger("core").info("Evaluasi diambil dari PGN (%d ply)", len(scores))
            update(path, eval_bar=True)
        elif engine := evaluation.find_engine():
            logging.getLogger("core").info("Menghitung evaluasi lewat engine (%s)", engine)
            scores = evaluation.from_engine(timeline, engine)
            (path / "evals.json").write_text(json.dumps(scores), encoding="utf-8")
            logging.getLogger("core").info("Evaluasi selesai (%d ply)", len(scores))
            update(path, eval_bar=True)
        else:
            logging.getLogger("core").info(
                "Tidak ada [%%eval] di PGN dan tidak ada engine ditemukan — lewati eval bar")

        update(path, status="new")

    background(project_id, work)
    return {"id": project_id, "status": "downloading"}


@app.get("/api/projects/{project_id}")
def status(project_id: str):
    path = folder(project_id)
    meta = read_meta(path)
    meta["busy"] = project_id in _running
    log = path / "log.txt"
    meta["log"] = log.read_text(encoding="utf-8", errors="replace")[-4000:] if log.is_file() else ""
    result = path / "timestamps.json"
    if result.is_file():
        data = json.loads(result.read_text(encoding="utf-8"))
        timeline = parse_pgn((path / "input.pgn").read_text(encoding="utf-8"))
        moves = {m["ply"]: m for m in timeline["moves"]}
        meta["waypoints"] = [
            {**w, "san": moves[w["ply"]]["san"], "side": moves[w["ply"]]["side"],
             "move_number": moves[w["ply"]]["move_number"]}
            for w in data["waypoints"] if w["ply"] in moves]
        meta["overlay_rect"] = data.get("overlay_rect")
    meta["outputs"] = [f.name for f in path.glob("*.mp4") if f.name != "source.mp4"]
    meta["themes"] = list(THEMES)
    meta["piece_sets"] = pieces.available_sets()
    meta.setdefault("theme", DEFAULT_THEME)
    meta.setdefault("piece_set", pieces.BUNDLED)
    meta.setdefault("sound", True)
    meta.setdefault("eval_bar", False)
    meta.setdefault("logo_rects", [])
    meta.setdefault("blur_rects", [])
    meta.setdefault("paste_rect", None)
    meta.setdefault("brand_file", None)
    meta.setdefault("brand_rect", None)
    meta.setdefault("name_rects", {"white": None, "black": None})
    meta["engine"] = str(engine) if (engine := evaluation.find_engine()) else None
    meta["has_evaluations"] = (path / "evals.json").is_file()
    return meta


@app.delete("/api/projects/{project_id}", status_code=204)
def delete(project_id: str):
    path = folder(project_id)
    if project_id in _running:
        raise HTTPException(409, "This project is already busy")
    shutil.rmtree(path)
    return Response(status_code=204)


class Look(BaseModel):
    theme: str | None = None
    piece_set: str | None = None
    sound: bool | None = None
    eval_bar: bool | None = None


@app.post("/api/projects/{project_id}/look")
def set_look(project_id: str, data: Look):
    path = folder(project_id)
    fields = {}
    if data.theme is not None:
        if data.theme not in THEMES:
            raise HTTPException(400, f"Unknown theme: {data.theme}")
        fields["theme"] = data.theme
    if data.piece_set is not None:
        if data.piece_set not in pieces.available_sets():
            raise HTTPException(400, f"Unknown piece set: {data.piece_set}")
        fields["piece_set"] = data.piece_set
    if data.sound is not None:
        fields["sound"] = bool(data.sound)
    if data.eval_bar is not None:
        fields["eval_bar"] = bool(data.eval_bar)
    update(path, **fields)
    return fields


@app.post("/api/projects/{project_id}/evaluate")
def start_evaluate(project_id: str):
    """Fill evals.json, from [%eval] comments if present, otherwise a local engine."""
    path = folder(project_id)
    text = (path / "input.pgn").read_text(encoding="utf-8")
    timeline = parse_pgn(text)
    if scores := evaluation.from_pgn(text, len(timeline["moves"])):
        (path / "evals.json").write_text(json.dumps(scores), encoding="utf-8")
        update(path, eval_bar=True)
        return {"source": "pgn", "plies": len(scores)}

    engine = evaluation.find_engine()
    if not engine:
        raise HTTPException(
            400, "No [%eval] comments in the PGN and no engine found. Put a Stockfish "
                 "binary in ./engines or on PATH, or export the PGN with evaluations.")
    update(path, status="evaluating", error=None)

    def work(path: Path):
        scores = evaluation.from_engine(timeline, engine)
        (path / "evals.json").write_text(json.dumps(scores), encoding="utf-8")
        update(path, status="ready", eval_bar=True)

    background(project_id, work)
    return {"source": "engine", "engine": str(engine)}


@app.post("/api/projects/{project_id}/detect")
def start_detect(project_id: str):
    path = folder(project_id)
    meta = read_meta(path)
    update(path, status="detecting", error=None)
    (path / "log.txt").write_text("", encoding="utf-8")

    def work(path: Path):
        detect(meta["video"], path / "input.pgn", output=path / "timestamps.json")
        update(path, status="ready")

    background(project_id, work)
    return {"status": "detecting"}


@app.post("/api/projects/{project_id}/board-quad")
def set_board_quad(project_id: str, quad: list | None = Body(None, embed=True)):
    """Where the wooden board sits in the frame: its 4 corners, [[x,y] x4] in source
    pixels, ordered top-left, top-right, bottom-right, bottom-left. Not axis-aligned --
    a camera angle rarely leaves the board square to the frame, so the selection
    follows its actual corners instead of a box."""
    path = folder(project_id)
    if quad is not None:
        if len(quad) != 4 or any(len(p) != 2 for p in quad):
            raise HTTPException(400, "quad must be 4 points: [[x,y], [x,y], [x,y], [x,y]]")
        quad = [[int(round(v)) for v in p] for p in quad]
        xs, ys = [p[0] for p in quad], [p[1] for p in quad]
        if max(xs) - min(xs) < 40 or max(ys) - min(ys) < 20:
            raise HTTPException(400, "Selection is too small to be a board")
    update(path, board_quad=quad)
    return {"board_quad": quad}


def pad_square(rect, video_w, video_h, growth=0.16):
    """Grow a detected (x, y, side) overlay square around its center, clamped to the
    frame. The detector locks onto the 8x8 grid exactly, but broadcasts often draw
    coordinate labels or a panel border just outside it, so a bare match leaves a
    visible sliver of the original overlay peeking out around our composited board."""
    x, y, side = rect
    if not video_w or not video_h:
        return rect
    grown = int(round(side * (1 + growth)))
    cx, cy = x + side / 2, y + side / 2
    nx = max(0, min(int(round(cx - grown / 2)), video_w - grown))
    ny = max(0, min(int(round(cy - grown / 2)), video_h - grown))
    grown = max(1, min(grown, video_w - nx, video_h - ny))
    return (nx, ny, grown)


def clamp_rects(rects, video_w, video_h):
    """Keep each [x, y, w, h] inside the frame -- independent x/w rounding can push
    a corner-hugging box a pixel past the edge, which ffmpeg's delogo rejects outright.
    delogo needs a 1px margin on every side: x and y must be >= 1, and x+w/y+h must be
    strictly less than the frame size, not just within it (confirmed empirically --
    the filter's own docs don't mention this margin)."""
    if not rects or not video_w or not video_h:
        return rects
    clamped = []
    for x, y, w, h in rects:
        x, y = max(1, min(x, video_w - 2)), max(1, min(y, video_h - 2))
        clamped.append([x, y, max(1, min(w, video_w - 1 - x)), max(1, min(h, video_h - 1 - y))])
    return clamped


def clean_rects(rects, meta, label="Box", minimum=10):
    """Validate, round and clamp a list of [x, y, w, h] against the frame."""
    cleaned = []
    for rect in rects:
        if len(rect) != 4 or any(not isinstance(v, (int, float)) for v in rect):
            raise HTTPException(400, "Each rect must be [x, y, w, h]")
        cleaned.append([int(round(v)) for v in rect])
    cleaned = clamp_rects(cleaned, meta.get("width"), meta.get("height"))
    for rect in cleaned:
        if rect[2] < minimum or rect[3] < minimum:
            raise HTTPException(400, f"{label} is too small")
    return cleaned


@app.post("/api/projects/{project_id}/logo-rects")
def set_logo_rects(project_id: str, rects: list = Body(..., embed=True)):
    """Boxes to blot out of the broadcast (channel bugs, watermarks, ...):
    each [x, y, w, h] in source pixels. Replaces the full list."""
    path = folder(project_id)
    cleaned = clean_rects(rects, read_meta(path), "Logo box")
    update(path, logo_rects=cleaned)
    return {"logo_rects": cleaned}


@app.post("/api/projects/{project_id}/blur-rects")
def set_blur_rects(project_id: str, rects: list = Body(..., embed=True)):
    """Boxes to blur rather than interpolate away. Interpolation (delogo) only looks
    right on a small static mark against a plain backdrop; anything that repaints every
    frame -- a broadcast's own eval bar, a ticker -- smears instead, so it gets blurred."""
    path = folder(project_id)
    cleaned = clean_rects(rects, read_meta(path), "Blur box")
    update(path, blur_rects=cleaned)
    return {"blur_rects": cleaned}


@app.post("/api/projects/{project_id}/paste-rect")
def set_paste_rect(project_id: str, rect: list | None = Body(None, embed=True)):
    """Where OUR rendered board gets pasted onto the broadcast, [x, y, w, h]. Null
    falls back to the detected overlay square. Distinct from `board_quad`, which is the
    wooden board being watched for timing -- this one is purely about output framing."""
    path = folder(project_id)
    if rect is not None:
        rect = clean_rects([rect], read_meta(path), "Board box", minimum=40)[0]
    update(path, paste_rect=rect)
    return {"paste_rect": rect}


@app.post("/api/projects/{project_id}/name-rects")
def set_name_rects(project_id: str, white: list | None = Body(None, embed=True),
                   black: list | None = Body(None, embed=True)):
    """Nameplate boxes for the two players, each [x, y, w, h] or null to drop it."""
    path = folder(project_id)
    meta = read_meta(path)
    plates = dict(meta.get("name_rects") or {})
    for side, rect in (("white", white), ("black", black)):
        plates[side] = clean_rects([rect], meta, "Nameplate")[0] if rect else None
    update(path, name_rects=plates)
    return {"name_rects": plates}


@app.post("/api/projects/{project_id}/brand-rect")
def set_brand_rect(project_id: str, rect: list | None = Body(None, embed=True)):
    """Where the uploaded logo image is drawn, [x, y, w, h]; null removes it."""
    path = folder(project_id)
    if rect is not None:
        rect = clean_rects([rect], read_meta(path), "Logo box")[0]
    update(path, brand_rect=rect)
    return {"brand_rect": rect}


@app.post("/api/projects/{project_id}/brand")
async def upload_brand(project_id: str, file: UploadFile = File(...)):
    """Store the user's own logo image alongside the project. PNG keeps transparency,
    which is what makes a logo sit on the footage instead of in a box."""
    path = folder(project_id)
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in (".png", ".jpg", ".jpeg", ".webp"):
        raise HTTPException(400, "Logo must be a .png, .jpg or .webp image")
    target = path / f"brand{suffix}"
    for stale in path.glob("brand.*"):
        stale.unlink()
    target.write_bytes(await file.read())
    try:
        with Image.open(target) as art:
            art.verify()
    except Exception:
        target.unlink(missing_ok=True)
        raise HTTPException(400, "That file is not a readable image")
    update(path, brand_file=target.name)
    return {"brand_file": target.name}


@app.delete("/api/projects/{project_id}/brand", status_code=204)
def delete_brand(project_id: str):
    path = folder(project_id)
    for stale in path.glob("brand.*"):
        stale.unlink()
    update(path, brand_file=None, brand_rect=None)
    return Response(status_code=204)


@app.post("/api/projects/{project_id}/retime")
def start_retime(project_id: str):
    path = folder(project_id)
    meta = read_meta(path)
    if not (path / "timestamps.json").is_file():
        raise HTTPException(400, "Run detection first")
    if not meta.get("board_quad"):
        raise HTTPException(400, "Select the physical board first")
    update(path, status="retiming", error=None)

    def work(path: Path):
        result = path / "timestamps.json"
        data = json.loads(result.read_text(encoding="utf-8"))
        data["waypoints"] = physical.refine(meta["video"], data["waypoints"],
                                            meta["board_quad"])
        data["source"] = "overlay+physical"
        data["board_quad"] = meta["board_quad"]
        result.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        update(path, status="ready")

    background(project_id, work)
    return {"status": "retiming"}


@app.post("/api/projects/{project_id}/waypoints")
def save_waypoints(project_id: str, waypoints: list = Body(..., embed=True)):
    path = folder(project_id)
    result = path / "timestamps.json"
    if not result.is_file():
        raise HTTPException(400, "Run detection first")
    data = json.loads(result.read_text(encoding="utf-8"))
    edited = {int(w["ply"]): w["timestamp"] for w in waypoints if w.get("timestamp") is not None}
    for point in data["waypoints"]:
        if point["ply"] in edited and point["timestamp"] != edited[point["ply"]]:
            point["timestamp"] = round(float(edited[point["ply"]]), 3)
            point["edited"] = True
    result.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return {"saved": len(edited)}


@app.post("/api/projects/{project_id}/render")
def start_render(project_id: str, full_video: bool = Body(True, embed=True)):
    path = folder(project_id)
    meta = read_meta(path)
    if not (path / "timestamps.json").is_file():
        raise HTTPException(400, "Run detection first")
    update(path, status="rendering", error=None)

    def work(path: Path):
        data = json.loads((path / "timestamps.json").read_text(encoding="utf-8"))
        timeline = parse_pgn((path / "input.pgn").read_text(encoding="utf-8"))
        plan = durations_from_waypoints(data["waypoints"])
        scores = None
        if meta.get("eval_bar") and (path / "evals.json").is_file():
            scores = json.loads((path / "evals.json").read_text(encoding="utf-8"))
        logging.getLogger("core").info(
            "Rendering %d segments (%s / %s%s)", len(plan), meta.get("theme"),
            meta.get("piece_set"), ", eval bar" if scores else "")
        render(timeline, plan, path / "board.mp4",
               size=fit_size(evaluation=bool(scores)),
               theme=meta.get("theme", DEFAULT_THEME),
               piece_set=meta.get("piece_set", pieces.BUNDLED),
               evaluations=scores)
        if meta.get("sound", True):
            logging.getLogger("core").info("Adding move sounds")
            track = audio.build_track(audio.events_from_plan(timeline, plan),
                                      sum(seconds for _, seconds in plan),
                                      path / "clicks.wav")
            staged = path / ".board-audio.mp4"
            audio.mux(path / "board.mp4", track, staged)
            staged.replace(path / "board.mp4")
        if full_video:
            size = (meta.get("width"), meta.get("height"))
            logo_rects = clamp_rects(meta.get("logo_rects"), *size)
            blur_rects = clamp_rects(meta.get("blur_rects"), *size)
            if paste := meta.get("paste_rect"):
                paste_rect = tuple(paste)
            else:
                x, y, side = pad_square(tuple(data["overlay_rect"]), *size)
                paste_rect = (x, y, side, side)

            furniture = None
            plates = meta.get("name_rects") or {}
            nameplates = [(meta.get(side), plates.get(side))
                          for side in ("white", "black") if plates.get(side)]
            brand = path / meta["brand_file"] if meta.get("brand_file") else None
            if nameplates or (brand and meta.get("brand_rect")):
                furniture = path / ".furniture.png"
                furniture_layer(size, brand=brand if meta.get("brand_rect") else None,
                                brand_rect=meta.get("brand_rect"), nameplates=nameplates,
                                theme=meta.get("theme", DEFAULT_THEME)).save(furniture)

            extras = [f"{len(logo_rects)} logo dihapus" if logo_rects else "",
                      f"{len(blur_rects)} area diblur" if blur_rects else "",
                      "logo + nama pemain" if furniture else ""]
            detail = ", ".join(x for x in extras if x)
            logging.getLogger("core").info(
                "Menimpa board overlay di video asli%s", f" ({detail})" if detail else "")
            overlay_composite(meta["video"], path / "board.mp4", path / "full-video.mp4",
                              paste_rect, logo_rects=logo_rects, blur_rects=blur_rects,
                              furniture=furniture)
        logging.getLogger("core").info("Render selesai")
        update(path, status="done")

    background(project_id, work)
    return {"status": "rendering"}


@app.get("/api/projects/{project_id}/frame")
def source_frame(project_id: str, t: float = 0.0, width: int = 640):
    meta = read_meta(folder(project_id))
    out = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", str(max(0.0, t)), "-i", meta["video"], "-frames:v", "1",
         "-vf", f"scale={width}:-2", "-f", "image2", "-c:v", "mjpeg", "-"],
        capture_output=True).stdout
    if not out:
        raise HTTPException(404, "No frame at that time")
    return Response(out, media_type="image/jpeg")


@app.get("/api/projects/{project_id}/video")
def source_video(project_id: str):
    """The source file itself. Served with Range support so the review pane can seek."""
    meta = read_meta(folder(project_id))
    video = Path(meta["video"])
    if not video.is_file():
        raise HTTPException(404, f"Video missing: {video}")
    return FileResponse(video, media_type="video/mp4")


@app.get("/api/projects/{project_id}/board")
def board_preview(project_id: str, ply: int = 0, width: int = 640,
                  theme: str | None = None, piece_set: str | None = None):
    import io
    path = folder(project_id)
    meta = read_meta(path)
    timeline = parse_pgn((path / "input.pgn").read_text(encoding="utf-8"))
    image = board_image(timeline, max(0, ply), size=(width, width),
                        theme=theme or meta.get("theme", DEFAULT_THEME),
                        piece_set=piece_set or meta.get("piece_set", pieces.BUNDLED))
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return Response(buffer.getvalue(), media_type="image/png")


@app.get("/api/projects/{project_id}/file/{name}")
def output_file(project_id: str, name: str):
    path = folder(project_id) / Path(name).name
    if not path.is_file():
        raise HTTPException(404, "Not found")
    return FileResponse(path)
