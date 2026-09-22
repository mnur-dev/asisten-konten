"""Local web app: one FastAPI process on 127.0.0.1, no auth, no queue, no workers."""
import json
import logging
import shutil
import subprocess
import threading
import re
import time
import uuid
from pathlib import Path

from fastapi import Body, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response
from PIL import Image
from pydantic import BaseModel

from core import audio, classify, evaluation, physical, pieces, thumbnail, titles
from core.detect import detect
from core.pgn import clock_series, has_clocks, parse_pgn
from core.render import (DEFAULT_THEME, SHORT_BLUR_DARKEN, SHORT_BLUR_SIGMA, SHORT_PAD,
                         SHORT_SIZE, SHORT_TAIL, THEMES, board_image, caption_window,
                         composite_frame, durations_from_waypoints, fit_size, furniture_layer, overlay_composite,
                         render, short_clip, short_reference_time, short_text_groups,
                         short_text_layer, short_top_height, zoom_crop_rect)
from core.video import download, make_preview, probe, source_info

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


def badges_on(meta: dict) -> bool:
    """Whether to paint move badges. They are on by default -- the grades are the
    point of running the evaluation, and leaving them off meant every project had to
    be told twice. A missing key is the default, so projects made before this reach
    it too; an explicit False from the Tampilan toggle always wins and stays off."""
    return bool(meta.get("move_badge", True))


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
            detail = str(error) or type(error).__name__   # some exceptions carry no message
            logging.getLogger("core").error("%s", detail)
            update(path, status="failed", error=detail)
        finally:
            logging.getLogger("core").removeHandler(handler)
            handler.close()
            _running.pop(project_id, None)

    if project_id in _running:
        raise HTTPException(409, "This project is already busy")
    thread = threading.Thread(target=runner, daemon=True)
    _running[project_id] = thread
    thread.start()


_previewing: set[str] = set()


def preview_video_of(project_id: str, path: Path, meta: dict) -> bool:
    """True when the scrubber's light copy (preview.mp4) is ready and newer than the
    source. Otherwise starts making it in its own thread -- not background(), which
    would mark the project busy and grey out every button for a file the user never
    asked for -- and the UI keeps using the source video until it lands. Skipped while
    the project is busy so it never competes with a download, detection or render."""
    preview = path / "preview.mp4"
    source = Path(meta.get("video") or "")
    if not source.is_file():
        return False
    if preview.is_file() and preview.stat().st_mtime >= source.stat().st_mtime:
        return True
    if project_id in _previewing or project_id in _running:
        return False
    _previewing.add(project_id)

    def work():
        try:
            make_preview(source, preview)
        except Exception as error:                      # the source video still works
            logging.getLogger("core").warning("preview.mp4 gagal dibuat: %s", error)
        finally:
            _previewing.discard(project_id)

    threading.Thread(target=work, daemon=True).start()
    return False


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
    for path in PROJECTS.iterdir():
        if (path / "meta.json").is_file():
            meta = read_meta(path)
            items.append({"id": path.name, "name": meta.get("name"), "status": meta.get("status"),
                         "created": meta.get("created", path.stat().st_ctime)})
    items.sort(key=lambda p: p["created"], reverse=True)
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
        "created": time.time(),
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
        "clock_box": has_clocks(timeline),
        "flip_board": False,
        "short_music": audio.default_music(),
        "short_music_offset": 0.0,
    })

    def work(path: Path):
        prepare_video(path, video, url, data.pgn_text, timeline)

    background(project_id, work)
    return {"id": project_id, "status": "downloading"}


def store_analysis(path: Path, timeline: dict, scores=None, positions=None) -> list:
    """Write evals.json (the bar) and moves.json (the grades) from one analysis.

    Both files come out of the same numbers, so they are written together and never
    drift apart. `positions` is analyse_game()'s richer record, one entry per
    position including the start; `scores` is the plain per-ply list a PGN's [%eval]
    comments give, and the position before move 1 is filled in with a book value
    because no PGN states it. Without `positions` there is no engine best move to
    compare against, so "best" and "great" simply never come up -- every other grade
    is unaffected."""
    if positions is None:
        positions = [classify.OPENING_SCORE] + list(scores)
        scores = list(scores)
    else:
        scores = evaluation.scores_from(positions)
    (path / "evals.json").write_text(json.dumps(scores), encoding="utf-8")
    records = classify.classify_game(timeline, positions)
    (path / "moves.json").write_text(json.dumps(records), encoding="utf-8")
    return scores


def classification_of(path: Path) -> list:
    """moves.json if this project has one, otherwise an empty list. Projects made
    before grading existed simply have no file and no badges until the evaluation
    is re-run."""
    grades = path / "moves.json"
    return json.loads(grades.read_text(encoding="utf-8")) if grades.is_file() else []


def prepare_video(path: Path, video: Path, url: str, pgn_text: str, timeline: dict):
    """Download the source video and line up its evaluations. Shared by project
    creation and the retry-download endpoint -- a download that failed partway
    through leaves the project in the same needs-video state either way."""
    logging.getLogger("core").info("Mengunduh video dari %s", url)
    last = -1

    def on_progress(percent, eta):
        nonlocal last
        rounded = round(percent)
        if rounded != last:
            last = rounded
            update(path, download_progress={"percent": rounded, "eta": eta})

    download(url, video, on_progress=on_progress)
    update(path, download_progress=None)
    info = probe(video)
    logging.getLogger("core").info("Unduh video selesai")
    update(path, width=info["width"], height=info["height"], duration=round(info["duration"], 2))

    if scores := evaluation.from_pgn(pgn_text, len(timeline["moves"])):
        store_analysis(path, timeline, scores)
        logging.getLogger("core").info("Evaluasi diambil dari PGN (%d ply)", len(scores))
        update(path, eval_bar=True)
    elif engine := evaluation.find_engine():
        logging.getLogger("core").info("Menghitung evaluasi lewat engine (%s)", engine)
        positions = evaluation.analyse_game(timeline, engine)
        scores = store_analysis(path, timeline, positions=positions)
        logging.getLogger("core").info("Evaluasi selesai (%d ply)", len(scores))
        update(path, eval_bar=True)
    else:
        logging.getLogger("core").info(
            "Tidak ada [%%eval] di PGN dan tidak ada engine ditemukan — lewati eval bar")

    update(path, status="new")


@app.post("/api/projects/{project_id}/retry-download")
def retry_download(project_id: str):
    """Re-run the download + evaluation step for a project whose initial download
    failed (network hiccup, YouTube throttling, ...) -- same video link and PGN,
    no need to recreate the project from scratch."""
    path = folder(project_id)
    meta = read_meta(path)
    url = meta.get("video_url")
    if not url:
        raise HTTPException(400, "This project has no video link on file")
    pgn_text = (path / "input.pgn").read_text(encoding="utf-8")
    timeline = parse_pgn(pgn_text)
    video = Path(meta["video"])
    update(path, status="downloading", error=None)

    def work(path: Path):
        prepare_video(path, video, url, pgn_text, timeline)

    background(project_id, work)
    return {"status": "downloading"}


SHORT_PLIES = 20            # closing plies a short covers when nothing is stored yet

LEAD_IN = 0.0               # seconds of broadcast kept before the first move


OUTRO = 60.0                # seconds of broadcast kept after the last move


def outro_of(meta: dict) -> float:
    """Seconds of broadcast kept after the last move -- the "selesai" box, mirror of
    lead_in. Replaces the old fixed rule (board.mp4's end + 60 s, i.e. last move
    + 63 s). 0 stops right on the last move; the default 60 keeps roughly the old
    length for projects that never set it. Capped by the broadcast's length."""
    try:
        value = meta.get("outro")
        return max(0.0, float(OUTRO if value is None else value))
    except (TypeError, ValueError):
        return OUTRO


def lead_in_of(meta: dict) -> float:
    """Seconds of broadcast kept in front of the first move. 0 -- the default, and
    what projects made before this existed fall back to -- means the long video opens
    on move 1, dropping the pre-game build-up nobody watches. A value of 60 keeps the
    last minute before it. Clamped against the first move's own timestamp at render
    time, so asking for more lead-in than the broadcast has simply starts at 0."""
    try:
        return max(0.0, float(meta.get("lead_in") or LEAD_IN))
    except (TypeError, ValueError):
        return LEAD_IN


def short_cut_of(meta: dict):
    """(plies, pad, tail) -- how the short is cut, as stored on the project. Every
    place that quotes a duration reads it from here, so the estimate, the caption
    timing table and the render can't drift apart; projects made before these were
    saved fall back to the same defaults the UI used to start with."""
    try:
        plies = max(1, int(meta.get("short_plies") or SHORT_PLIES))
    except (TypeError, ValueError):
        plies = SHORT_PLIES
    try:
        pad = max(0.1, float(meta.get("short_pad") or SHORT_PAD))
    except (TypeError, ValueError):
        pad = SHORT_PAD
    try:
        tail = max(0.0, float(meta.get("short_tail") or SHORT_TAIL))
    except (TypeError, ValueError):
        tail = SHORT_TAIL
    return plies, pad, tail


def short_music_of(meta: dict):
    """The short's background track: whatever the project stored, or DEFAULT_MUSIC
    for projects created before the setting existed. Tested with `in` rather than
    `.get()` because an explicit "(tanpa musik)" is stored as null and must stay
    null -- otherwise the default would silently come back every render."""
    return meta["short_music"] if "short_music" in meta else audio.default_music()


@app.get("/api/projects/{project_id}")
def status(project_id: str):
    path = folder(project_id)
    meta = read_meta(path)
    meta["busy"] = project_id in _running
    meta["preview_video"] = preview_video_of(project_id, path, meta)
    log = path / "log.txt"
    meta["log"] = log.read_text(encoding="utf-8", errors="replace")[-4000:] if log.is_file() else ""
    grades = classification_of(path)
    result = path / "timestamps.json"
    if result.is_file():
        data = json.loads(result.read_text(encoding="utf-8"))
        timeline = parse_pgn((path / "input.pgn").read_text(encoding="utf-8"))
        moves = {m["ply"]: m for m in timeline["moves"]}
        meta["waypoints"] = [
            {**w, "san": moves[w["ply"]]["san"], "side": moves[w["ply"]]["side"],
             "move_number": moves[w["ply"]]["move_number"],
             "label": (grades[w["ply"] - 1] or {}).get("label") if w["ply"] <= len(grades) else None,
             "loss": (grades[w["ply"] - 1] or {}).get("loss") if w["ply"] <= len(grades) else None}
            for w in data["waypoints"] if w["ply"] in moves]
        meta["overlay_rect"] = data.get("overlay_rect")
    # source.mp4 is the input and preview.mp4 the scrubber's proxy -- neither is output
    meta["outputs"] = [f.name for f in path.glob("*.mp4")
                       if f.name not in ("source.mp4", "preview.mp4") and not f.name.endswith(".partial.mp4")]
    meta["themes"] = list(THEMES)
    meta["piece_sets"] = pieces.available_sets()
    meta.setdefault("theme", DEFAULT_THEME)
    meta.setdefault("piece_set", pieces.BUNDLED)
    meta.setdefault("sound", True)
    meta.setdefault("eval_bar", False)
    meta.setdefault("clock_box", False)
    meta.setdefault("flip_board", False)
    # cheap enough to re-read every poll, and it keeps projects made before the clock
    # box existed from needing a migration
    meta["has_clocks"] = "[%clk" in (path / "input.pgn").read_text(encoding="utf-8")
    meta.setdefault("logo_rects", [])
    meta.setdefault("blur_rects", [])
    meta.setdefault("paste_rect", None)
    meta.setdefault("brand_file", None)
    meta.setdefault("brand_rect", None)
    meta.setdefault("download_progress", None)
    meta.setdefault("source_zoom", None)
    meta.setdefault("name_rects", {"white": None, "black": None})
    meta.setdefault("short_board_rect", None)
    meta.setdefault("short_texts", [])
    meta["short_music"] = short_music_of(meta)
    # the preview player plays this at each caption start, matching the render
    meta["caption_pop"] = audio.find_pop()
    meta.setdefault("short_music_offset", 0.0)
    plies, pad, tail = short_cut_of(meta)
    meta["short_plies"], meta["short_pad"], meta["short_tail"] = plies, pad, tail
    meta["window_set"] = "lead_in" in meta or "outro" in meta   # before the defaults fill them in
    meta["lead_in"] = lead_in_of(meta)
    meta["outro"] = outro_of(meta)
    meta.setdefault("thumb_time", None)
    meta.setdefault("thumb_texts", [])
    meta["thumb_prompt"] = thumb_prompt_of(meta)
    meta["thumb_default_prompt"] = thumb_prompt_of({**meta, "thumb_prompt": None})
    meta["thumb_attempts"] = thumb_attempts(path)
    meta["thumb_pick"] = thumb_pick_of(path, meta)
    meta["thumb_size"] = list(thumbnail.THUMB_SIZE)
    meta["has_thumb_source"] = (path / "thumb-source.png").is_file()
    meta["has_thumbnail"] = (path / "thumbnail.png").is_file()
    meta.setdefault("thumb_quality", thumbnail.DEFAULT_QUALITY)
    meta.setdefault("thumb_model", thumbnail.DEFAULT_MODEL)
    meta.setdefault("thumb_usage", [])
    meta["has_short_layout"] = has_short_layout(meta)
    meta["engine"] = str(engine) if (engine := evaluation.find_engine()) else None
    meta["has_evaluations"] = (path / "evals.json").is_file()
    meta["has_classification"] = bool(grades)
    # Shown as on only when it would actually paint something: with no grades yet the
    # toggle is disabled, and lighting it up would promise badges the board cannot have.
    meta["move_badge"] = badges_on(meta) and bool(grades)
    meta["classification"] = classify.summary(grades) if grades else None
    return meta


@app.delete("/api/projects")
def delete_all():
    """Wipe every project. Busy ones are left alone and named back to the UI."""
    deleted, busy = 0, []
    for path in PROJECTS.iterdir():
        if not (path / "meta.json").is_file():
            continue
        if path.name in _running:
            busy.append(read_meta(path).get("name") or path.name)
            continue
        shutil.rmtree(path, ignore_errors=True)
        deleted += 1
    return {"deleted": deleted, "busy": busy}


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
    clock_box: bool | None = None
    flip_board: bool | None = None
    move_badge: bool | None = None


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
    if data.clock_box is not None:
        fields["clock_box"] = bool(data.clock_box)
    if data.flip_board is not None:
        fields["flip_board"] = bool(data.flip_board)
    if data.move_badge is not None:
        fields["move_badge"] = bool(data.move_badge)
    update(path, **fields)
    return fields


@app.post("/api/projects/{project_id}/evaluate")
def start_evaluate(project_id: str):
    """Fill evals.json and moves.json, from [%eval] comments if present,
    otherwise from a local engine."""
    path = folder(project_id)
    text = (path / "input.pgn").read_text(encoding="utf-8")
    timeline = parse_pgn(text)
    if scores := evaluation.from_pgn(text, len(timeline["moves"])):
        store_analysis(path, timeline, scores)
        update(path, eval_bar=True)
        return {"source": "pgn", "plies": len(scores)}

    engine = evaluation.find_engine()
    if not engine:
        raise HTTPException(
            400, "No [%eval] comments in the PGN and no engine found. Put a Stockfish "
                 "binary in ./engines or on PATH, or export the PGN with evaluations.")
    update(path, status="evaluating", error=None)

    def work(path: Path):
        store_analysis(path, timeline, positions=evaluation.analyse_game(timeline, engine))
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


@app.post("/api/projects/{project_id}/source-zoom")
def set_source_zoom(project_id: str, zoom: dict | None = Body(None, embed=True)):
    """How far to punch in on the final composited frame, and where to centre that
    crop: {"percent": >=100, "x": 0-1, "y": 0-1}. Null resets to the untouched frame.
    Applied last in overlay_composite, after the board/logo/nameplates are already
    placed, so it reframes the whole picture without disturbing any of those boxes."""
    path = folder(project_id)
    if zoom is not None:
        try:
            percent = float(zoom["percent"])
            zx, zy = float(zoom["x"]), float(zoom["y"])
        except (KeyError, TypeError, ValueError):
            raise HTTPException(400, "zoom must be {percent, x, y}")
        if not 100 <= percent <= 500:
            raise HTTPException(400, "percent must be between 100 and 500")
        zoom = {"percent": percent, "x": min(max(zx, 0.0), 1.0), "y": min(max(zy, 0.0), 1.0)}
    update(path, source_zoom=zoom)
    return {"source_zoom": zoom}


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


def has_short_layout(meta: dict) -> bool:
    """Whether the short has been laid out at all -- a board box dragged, or at
    least one caption placed. Without either, short.mp4 would just be the centred
    default with no titles, which is never what someone actually wants, so the UI
    asks before falling back to rendering only the long video."""
    return bool(meta.get("short_board_rect") or meta.get("short_texts"))


def board_look_of(meta: dict, plan, grades=()) -> dict:
    """Everything board.mp4's pixels and timing depend on. Stored beside the file so
    the short button can tell a reusable board from a stale one: with the two renders
    split apart, changing the theme and then rendering only the short must not quietly
    stack the new captions on the old board.

    The grades themselves are part of it, not just the badge toggle: re-running the
    evaluation with an engine after a PGN-only pass repaints every badge, and a board
    rendered against the old labels would keep showing them."""
    look = {"theme": meta.get("theme", DEFAULT_THEME),
            "piece_set": meta.get("piece_set", pieces.BUNDLED),
            "eval_bar": bool(meta.get("eval_bar")),
            "clock_box": bool(meta.get("clock_box")),
            "flip_board": bool(meta.get("flip_board")),
            "sound": bool(meta.get("sound", True)),
            "plan": [[int(ply), round(float(seconds), 3)] for ply, seconds in plan]}
    # The badge keys appear only when badges are on AND there are grades to paint.
    # Either half missing means the board comes out the same as one rendered before
    # the feature existed, and adding the keys anyway would send the short button off
    # to rebuild it for a change that painted nothing.
    if badges_on(meta) and grades:
        look["move_badge"] = True
        look["badges"] = [g.get("label") for g in grades]
    return look


def build_board(path: Path, meta: dict, timeline, plan) -> None:
    """Render board.mp4 (plus its move clicks). Both render buttons need it: the long
    video pastes it over the broadcast, the short stacks it under one. Split out so
    the short can build it on demand when no earlier render left one behind."""
    scores = None
    if meta.get("eval_bar") and (path / "evals.json").is_file():
        scores = json.loads((path / "evals.json").read_text(encoding="utf-8"))
    clocks = clock_series(timeline) if meta.get("clock_box") else None
    orientation = "black" if meta.get("flip_board") else "white"
    grades = classification_of(path)
    badges = [g.get("label") for g in grades] if badges_on(meta) else None
    logging.getLogger("core").info(
        "Rendering %d segments (%s / %s%s%s%s%s)", len(plan), meta.get("theme"),
        meta.get("piece_set"), ", eval bar" if scores else "",
        ", jam" if clocks else "", ", badge langkah" if badges else "",
        ", papan dibalik" if orientation == "black" else "")
    render(timeline, plan, path / "board.mp4",
           size=fit_size(evaluation=bool(scores), clocks=bool(clocks)),
           theme=meta.get("theme", DEFAULT_THEME),
           piece_set=meta.get("piece_set", pieces.BUNDLED),
           evaluations=scores, clocks=clocks, orientation=orientation,
           classifications=badges)
    if meta.get("sound", True):
        logging.getLogger("core").info("Adding move sounds")
        track = audio.build_track(audio.events_from_plan(timeline, plan),
                                  sum(seconds for _, seconds in plan),
                                  path / "clicks.wav")
        staged = path / ".board-audio.mp4"
        audio.mux(path / "board.mp4", track, staged)
        staged.replace(path / "board.mp4")
    update(path, board_look=board_look_of(meta, plan, grades))


def build_short(path: Path, meta: dict, data: dict, plies: int, pad: float,
                tail: float = SHORT_TAIL) -> None:
    """Render short.mp4 from an already-rendered board.mp4 -- the caller guarantees
    one exists (build_board() if need be)."""
    plan = durations_from_waypoints(data["waypoints"])
    logging.getLogger("core").info(
        "Membuat video short (%d ply terakhir, %.2gs per sisi%s)", plies, pad,
        f", +{tail:.2g}s setelah ply terakhir" if tail > 0 else "")
    # one PNG per caption timing group, handed to short_clip as timed overlays
    layers, pop_times = [], set()
    for index, (start, end, items) in enumerate(short_text_groups(meta.get("short_texts"))):
        layer_path = path / f".short-text-{index}.png"
        short_text_layer(SHORT_SIZE, items).save(layer_path)
        layers.append((layer_path, start, end))
        pop_times.add(start)
    short_clip(meta["video"], path / "board.mp4", path / "short.mp4", data["waypoints"], plies,
               board_rect=meta.get("short_board_rect"), text_layers=layers,
               pad=pad, tail=tail)
    # a pop the moment each caption appears -- mixed in before the music so the
    # music's own level is the last thing applied on top of everything
    if pop_times:
        wav = path / ".short-pops.wav"
        length = probe(path / "short.mp4")["duration"]
        if audio.pop_track(sorted(pop_times), length, wav):
            logging.getLogger("core").info("Menambahkan pop di %d awal caption", len(pop_times))
            staged = path / ".short-pop.mp4"
            audio.add_track(path / "short.mp4", wav, staged)
            staged.replace(path / "short.mp4")
    if music := short_music_of(meta):
        logging.getLogger("core").info("Menambahkan musik latar (%s)", music)
        staged = path / ".short-music.mp4"
        audio.add_music(path / "short.mp4", audio.ASSETS_DIR / music, staged,
                        offset=meta.get("short_music_offset", 0.0))
        staged.replace(path / "short.mp4")



def long_layout(path: Path, meta: dict, data: dict, furniture_path: Path | None = None):
    """Everything the long video's composite needs from the project's layout: frame
    size, where the board goes, logo/blur boxes and the furniture PNG (channel logo +
    nameplates, None when there is neither). Used by the render and by the step-5
    preview frames, so the two can't disagree."""
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
        furniture = furniture_path or path / ".furniture.png"
        furniture_layer(size, brand=brand if meta.get("brand_rect") else None,
                        brand_rect=meta.get("brand_rect"), nameplates=nameplates).save(furniture)
    return size, paste_rect, logo_rects, blur_rects, furniture


@app.get("/api/projects/{project_id}/final-frame")
def final_frame(project_id: str, t: float, width: int = 960):
    """One frame of the long video as it WILL render, at broadcast second `t`: the
    board drawn for the ply on screen then (same look as board.mp4: theme, pieces,
    eval bar, clocks, badge, flip) composited with the current layout. No render needed."""
    import tempfile
    path = folder(project_id)
    meta = read_meta(path)
    if not (path / "timestamps.json").is_file():
        raise HTTPException(400, "Run detection first")
    data = json.loads((path / "timestamps.json").read_text(encoding="utf-8"))
    timeline = parse_pgn((path / "input.pgn").read_text(encoding="utf-8"))
    plan = durations_from_waypoints(data["waypoints"])
    elapsed, ply = 0.0, 0
    for index, seconds in plan[:-1]:          # plan[k] shows ply k until the next move lands
        if elapsed + seconds > t:
            break
        elapsed += seconds
        ply = index + 1
    ply = min(ply, len(timeline["moves"]))
    scores = None
    if meta.get("eval_bar") and (path / "evals.json").is_file():
        evals = json.loads((path / "evals.json").read_text(encoding="utf-8"))
        scores = evals[ply - 1] if 0 < ply <= len(evals) else {"cp": 0}
    series = clock_series(timeline) if meta.get("clock_box") else None
    grades = classification_of(path)
    badge = (grades[ply - 1].get("label") if badges_on(meta) and 0 < ply <= len(grades) else None)
    board = board_image(timeline, ply, fit_size(evaluation=bool(scores is not None), clocks=bool(series)),
                        orientation="black" if meta.get("flip_board") else "white",
                        theme=meta.get("theme", DEFAULT_THEME), piece_set=meta.get("piece_set", pieces.BUNDLED),
                        evaluation=scores, clocks=series[min(ply, len(series) - 1)] if series else None,
                        classification=badge)
    with tempfile.TemporaryDirectory() as scratch:
        board_png = Path(scratch) / "board.png"
        board.save(board_png)
        # five of these run at once, so the furniture PNG lives in this request's scratch dir
        size, paste_rect, logo_rects, blur_rects, furniture = long_layout(
            path, meta, data, furniture_path=Path(scratch) / "furniture.png")
        jpeg = composite_frame(meta["video"], board_png, t, paste_rect, logo_rects=logo_rects,
                               blur_rects=blur_rects, furniture=furniture,
                               zoom=meta.get("source_zoom"), size=size, width=max(160, min(width, 1920)))
    return Response(jpeg, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@app.post("/api/projects/{project_id}/render")
def start_render(project_id: str, full_video: bool = Body(True, embed=True)):
    """Render the long video: board.mp4, then full-video.mp4 pasted over the
    broadcast. The short is NOT built here -- it has its own button and endpoint, so
    a look change and a cut change no longer drag each other along.

    full-video.mp4 opens on the first move, or `lead_in` seconds before it (see
    lead_in_of). board.mp4 is still rendered from second 0 of the broadcast so the
    short can keep reusing it unchanged; only the composite is trimmed."""
    path = folder(project_id)
    meta = read_meta(path)
    if not (path / "timestamps.json").is_file():
        raise HTTPException(400, "Run detection first")
    update(path, status="rendering", error=None)

    def work(path: Path):
        data = json.loads((path / "timestamps.json").read_text(encoding="utf-8"))
        timeline = parse_pgn((path / "input.pgn").read_text(encoding="utf-8"))
        plan = durations_from_waypoints(data["waypoints"])
        build_board(path, meta, timeline, plan)
        if full_video:
            size, paste_rect, logo_rects, blur_rects, furniture = long_layout(path, meta, data)

            # plan[0] holds the starting position until the first move lands, so its
            # duration IS the first move's timestamp on the broadcast's clock. Both
            # the broadcast and board.mp4 run on that clock, so one seek trims both.
            first_move = plan[0][1]
            lead_in = lead_in_of(meta)
            start = max(0.0, first_move - lead_in)
            # every entry but the last (board.mp4's short closing hold) is time up to
            # the next move, so their sum is when the final move lands
            last_move = sum(seconds for _, seconds in plan[:-1])
            outro = outro_of(meta)
            end = max(start + 1.0, last_move + outro)
            if meta.get("duration"):
                end = min(end, float(meta["duration"]))

            zoom = meta.get("source_zoom")
            extras = [f"{len(logo_rects)} logo dihapus" if logo_rects else "",
                      f"{len(blur_rects)} area diblur" if blur_rects else "",
                      "logo + nama pemain" if furniture else "",
                      f"zoom {zoom['percent']:g}%" if zoom and zoom.get("percent", 100) > 100 else "",
                      f"mulai {start:.1f}s ({lead_in:.0f}s sebelum langkah 1)" if start > 0 else "",
                      "mulai dari langkah 1" if start == 0 and first_move > 0 else "",
                      f"selesai {end:.1f}s ({outro:.0f}s setelah langkah terakhir)"]
            detail = ", ".join(x for x in extras if x)
            logging.getLogger("core").info(
                "Menimpa board overlay di video asli%s", f" ({detail})" if detail else "")
            overlay_composite(meta["video"], path / "board.mp4", path / "full-video.mp4",
                              paste_rect, logo_rects=logo_rects, blur_rects=blur_rects,
                              furniture=furniture, zoom=meta.get("source_zoom"), size=size,
                              start=start, end=end)
        logging.getLogger("core").info("Render selesai")
        update(path, status="done")

    background(project_id, work)
    return {"status": "rendering"}


@app.post("/api/projects/{project_id}/short")
def start_short(project_id: str, rebuild_board: bool = Body(False, embed=True)):
    """Render short.mp4 on its own -- the long video is never touched. board.mp4 is
    reused when an earlier render left one that still matches the current look (see
    board_look_of), so trying another cut -- fewer plies, a tighter pad, a longer tail
    -- costs seconds instead of a full re-render; it is rebuilt here when missing,
    stale, or when `rebuild_board` forces it. How the short is cut comes from the
    project (see short_cut_of), so the length it renders is the one every panel has
    been quoting."""
    path = folder(project_id)
    meta = read_meta(path)
    if not (path / "timestamps.json").is_file():
        raise HTTPException(400, "Run detection first")
    plies, pad, tail = short_cut_of(meta)
    update(path, status="rendering", error=None)

    def work(path: Path):
        data = json.loads((path / "timestamps.json").read_text(encoding="utf-8"))
        plan = durations_from_waypoints(data["waypoints"])
        # a board from before board_look was recorded has unknown provenance; reuse it
        # rather than making every old project pay for a rebuild on its first short
        stale = ("board_look" in meta
                 and meta["board_look"] != board_look_of(meta, plan, classification_of(path)))
        if rebuild_board or stale or not (path / "board.mp4").is_file():
            logging.getLogger("core").info("Membuat board.mp4 dulu (belum ada / tampilannya berubah)")
            build_board(path, meta, parse_pgn((path / "input.pgn").read_text(encoding="utf-8")), plan)
        build_short(path, meta, data, plies, pad, tail)
        logging.getLogger("core").info("Short selesai")
        update(path, status="done")

    background(project_id, work)
    return {"status": "rendering"}


@app.get("/api/sounds")
def list_sounds():
    """Music files available for a short's background track (assets/sounds, minus
    the move-click clips), each with its duration for sizing the offset slider."""
    return {"music": [{"name": name, "duration": audio.probe_duration(audio.ASSETS_DIR / name)}
                      for name in audio.available_music()]}


@app.get("/api/sounds/{name}")
def sound_file(name: str):
    """Serves a music file for in-browser preview -- the offset picker seeks this
    with the <audio> element's own currentTime rather than round-tripping to the
    server on every drag."""
    target = audio.ASSETS_DIR / Path(name).name
    allowed = set(audio.available_music()) | {audio.find_pop() or ""}
    if not target.is_file() or target.name not in allowed:
        raise HTTPException(404, "Not found")
    return FileResponse(target)


@app.post("/api/projects/{project_id}/outro")
def set_outro(project_id: str, seconds: float = Body(..., embed=True)):
    """How many seconds of broadcast the long video keeps after the last move.
    0 stops on the last move. Saved just before each render, like lead-in."""
    path = folder(project_id)
    if seconds < 0:
        raise HTTPException(400, "outro must not be negative")
    seconds = round(float(seconds), 2)
    update(path, outro=seconds)
    return {"outro": seconds}


@app.post("/api/projects/{project_id}/lead-in")
def set_lead_in(project_id: str, seconds: float = Body(..., embed=True)):
    """How many seconds of broadcast the long video keeps before the first move.
    0 opens on move 1. Stored on the project like the short's cut, so the value
    survives a reload and the render never depends on what is left in the box."""
    path = folder(project_id)
    if seconds < 0:
        raise HTTPException(400, "lead-in must not be negative")
    seconds = round(float(seconds), 2)
    update(path, lead_in=seconds)
    return {"lead_in": seconds}


@app.post("/api/projects/{project_id}/short-cut")
def set_short_cut(project_id: str, plies: int = Body(..., embed=True),
                  pad: float = Body(..., embed=True), tail: float = Body(..., embed=True)):
    """How the short is cut: how many closing plies it covers, how many seconds are
    kept either side of each one, and how long the closing position is held after the
    last. Stored on the project rather than living in the input boxes, so the duration
    every other panel works against -- the estimate, the caption timing table, the
    render itself -- is one number that survives a reload."""
    path = folder(project_id)
    if plies < 1:
        raise HTTPException(400, "plies must be at least 1")
    if pad <= 0:
        raise HTTPException(400, "pad must be greater than 0")
    if tail < 0:
        raise HTTPException(400, "tail must not be negative")
    plies, pad, tail = int(plies), round(float(pad), 2), round(float(tail), 2)
    update(path, short_plies=plies, short_pad=pad, short_tail=tail)
    return {"short_plies": plies, "short_pad": pad, "short_tail": tail}


@app.post("/api/projects/{project_id}/short-music")
def set_short_music(project_id: str, name: str | None = Body(None, embed=True),
                    offset: float = Body(0.0, embed=True)):
    """The background music track for the short and where in it to start (seconds),
    so the user can drag past an intro to line up a drop/hook with the highlight."""
    path = folder(project_id)
    if name is None:
        offset = 0.0
    elif name not in audio.available_music():
        raise HTTPException(400, "Unknown music file")
    else:
        offset = max(0.0, float(offset))
    update(path, short_music=name, short_music_offset=offset)
    return {"short_music": name, "short_music_offset": offset}


@app.post("/api/projects/{project_id}/short-board-rect")
def set_short_board_rect(project_id: str, rect: list | None = Body(None, embed=True)):
    """Where the board sits in the short's vertical canvas, [x, y, w, h] in canvas
    pixels (the canvas is a fixed SHORT_SIZE, not the source video's own resolution).
    Null falls back to the centred default (see default_short_board_rect)."""
    path = folder(project_id)
    canvas = {"width": SHORT_SIZE[0], "height": SHORT_SIZE[1]}
    if rect is not None:
        rect = clean_rects([rect], canvas, "Board box", minimum=40)[0]
    update(path, short_board_rect=rect)
    return {"short_board_rect": rect}


@app.post("/api/projects/{project_id}/short-texts")
def set_short_texts(project_id: str, texts: list = Body(..., embed=True)):
    """Caption boxes for the short: each {"text": str, "rect": [x, y, w, h],
    "scale": float, "start": float, "end": float | None} in canvas pixels. `scale`
    (default 1.0) sizes the caption up or down from its auto-fit size -- the
    text-size control. `start`/`end` are seconds in the short's own timeline, so a
    caption can come and go partway through; `end` null keeps it up to the end.
    Replaces the full list."""
    path = folder(project_id)
    canvas = {"width": SHORT_SIZE[0], "height": SHORT_SIZE[1]}
    cleaned = []
    for item in texts:
        text = str(item.get("text", "")).strip()[:200]
        if not text:
            raise HTTPException(400, "Text box needs non-empty text")
        if not isinstance(item.get("rect"), list):
            raise HTTPException(400, "Each text box needs a rect [x, y, w, h]")
        rect = clean_rects([item["rect"]], canvas, "Text box", minimum=20)[0]
        try:
            scale = min(2.5, max(0.4, float(item.get("scale", 1.0))))
        except (TypeError, ValueError):
            scale = 1.0
        start, end = caption_window(item)
        cleaned.append({"text": text, "rect": rect, "scale": scale,
                        "start": round(start, 2), "end": None if end is None else round(end, 2)})
    # Every caption shares one box: the first one's. Enforced here rather than only
    # in the editor so the invariant holds whichever path saves -- adding a caption,
    # retiming one, resizing one -- and the box drawn over the frame is never a
    # stand-in for four boxes that quietly drifted a few pixels apart.
    for item in cleaned:
        item["rect"] = cleaned[0]["rect"]
    update(path, short_texts=cleaned)
    return {"short_texts": cleaned}


@app.get("/api/projects/{project_id}/short-text-preview")
def short_text_preview(project_id: str, width: int = 300, only: str = ""):
    """The caption layer rendered by the exact same short_text_layer() short_clip()
    uses, just scaled down -- so sizing a caption in the browser preview matches the
    real render pixel-for-pixel instead of an approximated CSS rendering, which is
    what made it hard to land on the right size before.

    `only` is a comma-separated list of caption indices. The preview player asks for
    one image per timing group and cross-fades between them as the playhead moves,
    which is how captions that come and go show up there without re-rendering a
    layer on every frame."""
    import io
    meta = read_meta(folder(project_id))
    canvas_w, canvas_h = SHORT_SIZE
    out_w = max(2, width - width % 2)
    out_h = round(canvas_h * out_w / canvas_w / 2) * 2
    texts = meta.get("short_texts") or []
    if only.strip():
        wanted = {int(part) for part in only.split(",") if part.strip().isdigit()}
        texts = [item for index, item in enumerate(texts) if index in wanted]
    layer = short_text_layer(SHORT_SIZE, texts)
    layer = layer.resize((out_w, out_h), Image.LANCZOS)
    buffer = io.BytesIO()
    layer.save(buffer, "PNG")
    return Response(buffer.getvalue(), media_type="image/png")


@app.get("/api/projects/{project_id}/short-frame")
def short_frame(project_id: str, plies: int = 20, width: int = 360):
    """Preview of the short's canvas: the source video letterboxed at the top,
    exactly where it lands in the final render, over the same blurred backdrop
    short_clip() fills the empty areas with -- the layout picker draws the
    board/text boxes over this on the client side."""
    path = folder(project_id)
    meta = read_meta(path)
    if not (path / "timestamps.json").is_file():
        raise HTTPException(400, "Run detection first")
    data = json.loads((path / "timestamps.json").read_text(encoding="utf-8"))
    t = short_reference_time(data["waypoints"], max(1, plies))

    canvas_w, canvas_h = SHORT_SIZE
    scale = width / canvas_w
    out_w = width - width % 2
    out_h = round(canvas_h * scale / 2) * 2
    top_h = short_top_height(out_w, meta["width"], meta["height"])
    sigma = max(4, round(SHORT_BLUR_SIGMA * scale))
    out = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", str(max(0.0, t)), "-i", meta["video"], "-frames:v", "1",
         "-filter_complex",
         f"[0:v]scale={out_w}:{out_h}:force_original_aspect_ratio=increase,"
         f"crop={out_w}:{out_h},gblur=sigma={sigma},eq=brightness=-{SHORT_BLUR_DARKEN}[bg];"
         f"[0:v]scale={out_w}:{top_h}[top];[bg][top]overlay=0:0",
         "-f", "image2", "-c:v", "mjpeg", "-"],
        capture_output=True).stdout
    if not out:
        raise HTTPException(404, "No frame at that time")
    return Response(out, media_type="image/jpeg")


@app.get("/api/projects/{project_id}/frame")
def source_frame(project_id: str, t: float = 0.0, width: int = 640, zoomed: bool = False):
    """`zoomed` previews the source_zoom crop -- used by the layout picker (paste/logo/
    blur/brand/nameplate boxes), which are drawn against that already-zoomed frame since
    overlay_composite applies the same crop before placing any of them. The physical-board
    picker never sets it: that detection reads the untouched broadcast directly."""
    meta = read_meta(folder(project_id))
    filters = []
    zoom = meta.get("source_zoom")
    if zoomed and zoom and zoom.get("percent", 100) > 100 and meta.get("width") and meta.get("height"):
        crop_w, crop_h, crop_x, crop_y = zoom_crop_rect((meta["width"], meta["height"]), zoom)
        filters.append(f"crop={crop_w}:{crop_h}:{crop_x}:{crop_y}")
    filters.append(f"scale={width}:-2")
    out = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", str(max(0.0, t)), "-i", meta["video"], "-frames:v", "1",
         "-vf", ",".join(filters), "-f", "image2", "-c:v", "mjpeg", "-"],
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


# Manual upload package: the app does not upload (the Cloud project is unaudited, so
# API uploads would be locked private). It prepares per-channel title/description/
# tags to copy into YouTube Studio or the app, next to the files to download.
UPLOAD_CHANNELS = {
    "pawn-initiate": "Pawn Initiate",
    "checkmate-theater": "Checkmate Theater",
}
UPLOAD_TEMPLATES = ROOT / "upload-templates.json"


def upload_templates() -> dict:
    """Default description + tags per channel. Gitignored (contact email, donation
    links), editable from the UI; a channel missing from the file starts blank."""
    try:
        stored = json.loads(UPLOAD_TEMPLATES.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        stored = {}
    return {slug: {"name": name, "description": stored.get(slug, {}).get("description", ""),
                   "tags": stored.get(slug, {}).get("tags", [])}
            for slug, name in UPLOAD_CHANNELS.items()}


@app.get("/api/upload-templates")
def get_upload_templates():
    return {"channels": upload_templates()}


class UploadText(BaseModel):
    description: str = ""
    tags: list[str] = []


@app.post("/api/upload-templates/{slug}")
def set_upload_template(slug: str, body: UploadText):
    """Make this description + tags the channel's default for every project."""
    if slug not in UPLOAD_CHANNELS:
        raise HTTPException(404, "Unknown channel")
    try:
        stored = json.loads(UPLOAD_TEMPLATES.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        stored = {}
    stored[slug] = {"description": body.description, "tags": [t for t in body.tags if t.strip()]}
    UPLOAD_TEMPLATES.write_text(json.dumps(stored, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"ok": True}


class TitleRequest(BaseModel):
    kind: str = "long"                # "long" | "short"
    channel: str = "pawn-initiate"


@app.post("/api/projects/{project_id}/title-suggestions")
def title_suggestions(project_id: str, body: TitleRequest):
    """Five titles from Claude Code, combining the source video's title, the PGN and
    the channel's measured patterns (core/titles.py). Runs in the request (FastAPI
    puts plain `def` endpoints on a worker thread); takes ~10-40 s. The source title
    is looked up once with yt-dlp and cached in meta.json."""
    if body.kind not in ("long", "short"):
        raise HTTPException(400, "kind must be long or short")
    path = folder(project_id)
    meta = read_meta(path)
    if not (titles.PATTERNS / f"{body.channel}.md").is_file():
        raise HTTPException(400, f"Belum ada pola judul untuk channel {body.channel}")
    if not meta.get("source_title") and meta.get("video_url"):
        info = source_info(meta["video_url"])
        if info:
            meta = update(path, source_title=info["title"], source_channel=info["channel"])
    try:
        result = titles.suggest(path, meta, body.kind, body.channel)
    except Exception as error:
        raise HTTPException(502, str(error) or type(error).__name__)
    stored = meta.get("title_suggestions") or {}
    stored[f"{body.channel}:{body.kind}"] = {"titles": result, "created": time.strftime("%Y-%m-%d %H:%M")}
    update(path, title_suggestions=stored)
    return {"titles": result, "source_title": meta.get("source_title")}


class UploadDraft(BaseModel):
    channel: str
    file: str
    title: str = ""
    description: str = ""
    tags: list[str] = []


@app.post("/api/projects/{project_id}/upload-draft")
def set_upload_draft(project_id: str, body: UploadDraft):
    """This project's title/description/tags for one channel + file, kept in
    meta.json under upload_drafts["<channel>:<file>"] so a reload keeps them."""
    if body.channel not in UPLOAD_CHANNELS:
        raise HTTPException(404, "Unknown channel")
    path = folder(project_id)
    drafts = read_meta(path).get("upload_drafts") or {}
    drafts[f"{body.channel}:{body.file}"] = {
        "title": body.title, "description": body.description,
        "tags": [t for t in body.tags if t.strip()]}
    update(path, upload_drafts=drafts)
    return {"ok": True}


@app.get("/api/projects/{project_id}/preview-video")
def preview_video(project_id: str):
    """The scrubber's light copy (see preview_video_of). 404 until it has been made."""
    preview = folder(project_id) / "preview.mp4"
    if not preview.is_file():
        raise HTTPException(404, "Preview video not ready")
    return FileResponse(preview, media_type="video/mp4")


@app.get("/api/projects/{project_id}/board")
def board_preview(project_id: str, ply: int = 0, width: int = 640,
                  theme: str | None = None, piece_set: str | None = None,
                  flip: bool | None = None):
    import io
    path = folder(project_id)
    meta = read_meta(path)
    timeline = parse_pgn((path / "input.pgn").read_text(encoding="utf-8"))
    ply = max(0, ply)
    clocks = None
    if meta.get("clock_box") and (series := clock_series(timeline)):
        clocks = series[min(ply, len(series) - 1)]
    flipped = meta.get("flip_board") if flip is None else flip
    grades = classification_of(path) if badges_on(meta) else []
    image = board_image(timeline, ply, size=(width, width),
                        orientation="black" if flipped else "white",
                        theme=theme or meta.get("theme", DEFAULT_THEME),
                        piece_set=piece_set or meta.get("piece_set", pieces.BUNDLED),
                        clocks=clocks,
                        classification=(grades[ply - 1].get("label")
                                        if 0 < ply <= len(grades) else None))
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return Response(buffer.getvalue(), media_type="image/png")


@app.get("/api/projects/{project_id}/file/{name}")
def output_file(project_id: str, name: str):
    path = folder(project_id) / Path(name).name
    if not path.is_file():
        raise HTTPException(404, "Not found")
    return FileResponse(path)


# ------------------------------------------------------------------ thumbnail

def thumb_model_record(model: str) -> dict | None:
    """The cached OpenRouter record for one model, or None if it is not in the
    list. Used to decide what parameters the model will actually accept."""
    try:
        return next((m for m in thumbnail.list_models() if m["id"] == model), None)
    except Exception as error:      # offline: let the call itself be the judge
        logging.getLogger("core").warning("Daftar model tidak terbaca: %s", error)
        return None


def thumb_prompt_of(meta: dict) -> str:
    """The prompt this project sends, with the player names filled in.

    Stored on the project only once the user edits it. That way improving
    DEFAULT_PROMPT still reaches every project that never touched its own, while a
    project whose prompt was hand-tuned keeps exactly what was tuned.
    """
    stored = (meta.get("thumb_prompt") or "").strip()
    if stored:
        return stored
    return thumbnail.DEFAULT_PROMPT.format(
        white=meta.get("white") or "the player on the left",
        black=meta.get("black") or "the player on the right",
        teks=(meta.get("thumb_text") or "").strip() or thumbnail.TEXT_PLACEHOLDER)


def thumb_attempts(path: Path) -> list[str]:
    """Every AI attempt this project has paid for, oldest first.

    Kept rather than overwritten: a later attempt is often worse than an earlier
    one, and going back to it must not cost another call.
    """
    def index(name: str) -> int:
        try:
            return int(name.rsplit("-", 1)[-1].removesuffix(".png"))
        except ValueError:
            return 0
    return sorted((p.name for p in path.glob("thumb-ai-*.png")), key=index)


def thumb_pick_of(path: Path, meta: dict) -> str | None:
    """Which attempt the finished thumbnail is built from -- the stored choice while
    its file still exists, otherwise the newest attempt."""
    attempts = thumb_attempts(path)
    pick = meta.get("thumb_pick")
    return pick if pick in attempts else (attempts[-1] if attempts else None)


@app.get("/api/openrouter-key")
def openrouter_key_status():
    """Whether an image-model key is reachable. Never returns the key itself."""
    return {"has_key": bool(thumbnail.find_api_key())}


@app.post("/api/openrouter-key")
def set_openrouter_key(key: str = Body(..., embed=True)):
    """Save the key to the gitignored .env at the repo root. Plain text on a machine
    only this user has -- the same place they would have put it by hand -- and read
    fresh on every call, so it works without restarting the server."""
    if not (key := (key or "").strip()):
        raise HTTPException(400, "Key kosong")
    thumbnail.save_api_key(key)
    return {"has_key": bool(thumbnail.find_api_key())}


@app.get("/api/openrouter/models")
def openrouter_models(refresh: bool = False):
    """Every image model that can take an input image, with a per-edit price.

    Served from a disk cache (see thumbnail.list_models): pricing lives on a
    per-model record, so building the list is one request per model. `refresh`
    forces a refetch -- new models then show up without a code change, which is
    the whole reason this goes through OpenRouter."""
    try:
        return {"models": thumbnail.list_models(refresh=refresh),
                "default": thumbnail.DEFAULT_MODEL}
    except Exception as error:
        raise HTTPException(502, f"Daftar model gagal diambil: {error}")


@app.post("/api/projects/{project_id}/thumb-frame")
def set_thumb_frame(project_id: str, seconds: float = Body(..., embed=True)):
    """Freeze one broadcast second as the thumbnail's source frame.

    Written to disk now rather than re-grabbed at generate time so that what gets
    uploaded is exactly the frame the user approved, and so retrying a prompt does
    not depend on the video file still being where it was.
    """
    path = folder(project_id)
    meta = read_meta(path)
    if seconds < 0:
        raise HTTPException(400, "seconds must not be negative")
    thumbnail.grab_frame(meta["video"], seconds, path / "thumb-source.png")
    seconds = round(float(seconds), 2)
    update(path, thumb_time=seconds)
    return {"thumb_time": seconds}


@app.post("/api/projects/{project_id}/thumb-prompt")
def set_thumb_prompt(project_id: str, prompt: str = Body("", embed=True)):
    """Store an edited prompt, or clear it back to the shared default with an
    empty string."""
    path = folder(project_id)
    update(path, thumb_prompt=(prompt or "").strip() or None)
    return {"thumb_prompt": thumb_prompt_of(read_meta(path))}


@app.post("/api/projects/{project_id}/thumb-text")
def set_thumb_text(project_id: str, text: str = Body("", embed=True)):
    """The text the AI paints on the thumbnail. Filled into the default prompt; in a
    hand-edited prompt the quoted text after 'tambahkan text' is swapped in place, so
    picking a new text never throws away the user's other edits."""
    path = folder(project_id)
    meta = read_meta(path)
    text = text.strip().strip('"')
    fields = {"thumb_text": text or None}
    if stored := (meta.get("thumb_prompt") or "").strip():
        # straight or curly quotes: prompts typed on a phone come with “ ”
        fields["thumb_prompt"] = re.sub(r'(tambahkan text\s*["“])[^"”]*(["”])',
                                        lambda m: m.group(1) + (text or thumbnail.TEXT_PLACEHOLDER) + m.group(2),
                                        stored, count=1)
    meta = update(path, **fields)
    return {"thumb_text": text, "thumb_prompt": thumb_prompt_of(meta)}


@app.post("/api/projects/{project_id}/thumb-text-suggestions")
def thumb_text_suggestions(project_id: str, title: str = Body(..., embed=True)):
    """Three thumbnail texts from Claude Code that complement the chosen title."""
    if not title.strip():
        raise HTTPException(400, "Pilih judul video dulu")
    path = folder(project_id)
    try:
        texts = titles.thumb_text_options(path, read_meta(path), title.strip())
    except Exception as error:
        raise HTTPException(502, str(error) or type(error).__name__)
    update(path, thumb_text_suggestions={"title": title.strip(), "texts": texts,
                                          "created": time.strftime("%Y-%m-%d %H:%M")})
    return {"texts": texts}


@app.post("/api/projects/{project_id}/thumb-texts")
def set_thumb_texts(project_id: str, texts: list = Body(..., embed=True)):
    """Headline boxes, in 1280x720 thumbnail pixels: each {"text", "rect", "scale"}.

    Drawn locally by PIL, so this is the free half: saving new text costs nothing
    and never touches the paid picture underneath.
    """
    path = folder(project_id)
    canvas = {"width": thumbnail.THUMB_SIZE[0], "height": thumbnail.THUMB_SIZE[1]}
    cleaned = []
    for item in texts:
        if not isinstance(item, dict):
            raise HTTPException(400, "Each text must be an object")
        rect = clean_rects([item.get("rect") or []], canvas, "Text box", minimum=20)[0]
        try:
            scale = min(3.0, max(0.3, float(item.get("scale") or 1.0)))
        except (TypeError, ValueError):
            scale = 1.0
        cleaned.append({"text": str(item.get("text") or ""), "rect": rect, "scale": scale})
    update(path, thumb_texts=cleaned)
    return {"thumb_texts": cleaned}


@app.post("/api/projects/{project_id}/thumb-compose")
def compose_thumbnail(project_id: str, pick: str | None = Body(None, embed=True)):
    """Redraw the headline over an attempt and save thumbnail.png.

    The free step, and the reason the text is not left to the image model: trying
    another title, size or position re-runs only this, in milliseconds, with the
    paid picture untouched underneath.
    """
    path = folder(project_id)
    meta = read_meta(path)
    if pick is not None and pick not in thumb_attempts(path):
        raise HTTPException(400, "Attempt tidak ada")
    if pick is not None:
        meta = update(path, thumb_pick=pick)
    if not (chosen := thumb_pick_of(path, meta)):
        raise HTTPException(400, "Belum ada gambar hasil AI")
    thumbnail.compose(path / chosen, meta.get("thumb_texts"), path / "thumbnail.png")
    update(path, thumb_pick=chosen)
    return {"thumb_pick": chosen}


@app.post("/api/projects/{project_id}/thumb-generate")
def start_thumbnail(project_id: str, quality: str | None = Body(thumbnail.DEFAULT_QUALITY, embed=True),
                    model: str = Body(thumbnail.DEFAULT_MODEL, embed=True)):
    """The one paid step: send the frozen frame plus the prompt to the image model.

    Runs in the background like the renders do -- an edit takes a minute or two, far
    past any sensible request timeout. The result lands as a new numbered attempt and
    is composed with the current headline straight away, so the panel shows a
    finished thumbnail rather than a bare picture.
    """
    path = folder(project_id)
    meta = read_meta(path)
    if not (path / "thumb-source.png").is_file():
        raise HTTPException(400, "Pilih frame-nya dulu")
    # Only some models take a quality parameter at all, and those that do accept
    # different sets. Sending one a model does not know is a rejected request, not
    # an ignored field, so the model's own record decides.
    record = thumb_model_record(model)
    allowed = (record or {}).get("qualities")
    if allowed and quality not in allowed:
        raise HTTPException(400, f"quality untuk {model} harus " + ", ".join(allowed))
    quality = quality if allowed else None
    if not (key := thumbnail.find_api_key()):
        raise HTTPException(400, "OPENROUTER_API_KEY belum diisi")
    prompt = thumb_prompt_of(meta)
    update(path, status="thumbnail", error=None, thumb_quality=quality, thumb_model=model)

    def work(path: Path):
        number = len(thumb_attempts(path)) + 1
        while (path / f"thumb-ai-{number}.png").exists():
            number += 1
        target = path / f"thumb-ai-{number}.png"
        logging.getLogger("core").info(
            "Membuat thumbnail (percobaan %d, %s, kualitas %s)", number, model, quality)
        usage = thumbnail.edit_image(path / "thumb-source.png", prompt, target, key,
                                     model=model, quality=quality)
        if usage:
            cost = usage.get("cost")
            logging.getLogger("core").info(
                "Biaya nyata panggilan ini: %s (%s)",
                f"${cost:.4f}" if isinstance(cost, (int, float)) else "tidak dilaporkan",
                json.dumps(usage))
        fresh = read_meta(path)
        spent = list(fresh.get("thumb_usage") or [])
        spent.append({"file": target.name, "model": model, "quality": quality,
                      "usage": usage, "at": time.strftime("%Y-%m-%d %H:%M")})
        thumbnail.compose(target, fresh.get("thumb_texts"), path / "thumbnail.png")
        update(path, thumb_usage=spent, thumb_pick=target.name, status="done")
        logging.getLogger("core").info("Thumbnail selesai: %s", target.name)

    background(project_id, work)
    return {"status": "thumbnail"}


@app.delete("/api/projects/{project_id}/thumb-attempt/{name}", status_code=204)
def delete_thumb_attempt(project_id: str, name: str):
    """Drop one attempt. The picture was paid for, so this is never automatic."""
    path = folder(project_id)
    if (name := Path(name).name) not in thumb_attempts(path):
        raise HTTPException(404, "Attempt tidak ada")
    (path / name).unlink()
    if read_meta(path).get("thumb_pick") == name:
        update(path, thumb_pick=thumb_pick_of(path, {}))
    return Response(status_code=204)
