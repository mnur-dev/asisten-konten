import os, sqlite3, uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from fastapi import FastAPI, Header, HTTPException, UploadFile, File, Form
from fastapi.responses import FileResponse
from pydantic import BaseModel
from controller.dashboard import add_routes
from shared.video_url import validate_video_url

app = FastAPI(title="Chess Automation Controller")
ROOT = Path(os.getenv("CHESS_STORAGE", Path(__file__).parents[1] / "storage"))
INPUTS, RESULTS, DB = ROOT / "inputs", ROOT / "results", ROOT / "jobs.db"
for path in (INPUTS, RESULTS): path.mkdir(parents=True, exist_ok=True)


def now(): return datetime.now(timezone.utc)
def auth(authorization):
    expected = os.getenv("WORKER_TOKEN")
    if not expected or authorization != f"Bearer {expected}": raise HTTPException(401, "Invalid worker token")


def db():
    connection = sqlite3.connect(DB)
    connection.row_factory = sqlite3.Row
    connection.execute("CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,status TEXT,created_at TEXT,claimed_at TEXT,started_at TEXT,completed_at TEXT,worker_id TEXT,error_message TEXT,pgn_path TEXT,result_path TEXT,timestamps_path TEXT,lease_expires_at TEXT,video_url TEXT,detected_timestamps_path TEXT)")
    columns = {row[1] for row in connection.execute("PRAGMA table_info(jobs)")}
    for name in ("timestamps_path", "lease_expires_at", "video_url", "detected_timestamps_path"):
        if name not in columns: connection.execute(f"ALTER TABLE jobs ADD COLUMN {name} TEXT")
    connection.commit()
    return connection


def row(job): return dict(job) if job else None
def lease_time(): return (now() + timedelta(seconds=int(os.getenv("LEASE_SECONDS", "120")))).isoformat()
def owned_job(connection, jid, worker_id, statuses):
    job = connection.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
    if not job: raise HTTPException(404, "Job not found")
    if job["worker_id"] != worker_id or job["status"] not in statuses: raise HTTPException(409, "Invalid job transition")
    return job


add_routes(app, db, INPUTS, RESULTS)


class Claim(BaseModel): worker_id: str
class WorkerAction(BaseModel): worker_id: str
class Status(WorkerAction): status: str; error_message: str | None = None


@app.get("/health")
def health(): return {"status": "ok"}


@app.post("/api/jobs", status_code=201)
def create(pgn_file: UploadFile = File(...), timestamps_file: UploadFile | None = File(None), video_url: str | None = Form(None), authorization: str | None = Header(None)):
    auth(authorization)
    jid = uuid.uuid4().hex
    pgn_path = INPUTS / f"{jid}.pgn"; pgn_path.write_bytes(pgn_file.file.read())
    timestamps_path = None
    if timestamps_file:
        timestamps_path = INPUTS / f"{jid}.timestamps.json"; timestamps_path.write_bytes(timestamps_file.file.read())
    connection = db()
    video_url = validate_video_url(video_url) if video_url else None
    connection.execute("INSERT INTO jobs(id,status,created_at,pgn_path,timestamps_path,video_url) VALUES(?,?,?,?,?,?)", (jid, "WAITING", now().isoformat(), str(pgn_path), str(timestamps_path) if timestamps_path else None, video_url))
    connection.commit()
    return {"id": jid, "status": "WAITING"}


@app.get("/api/jobs/next")
def next_job(authorization: str | None = Header(None)):
    auth(authorization); connection = db(); current = now().isoformat()
    connection.execute("UPDATE jobs SET status='WAITING',worker_id=NULL,claimed_at=NULL,started_at=NULL,lease_expires_at=NULL,error_message='Worker lease expired; job requeued' WHERE status IN ('CLAIMED','PROCESSING','UPLOADING') AND lease_expires_at < ?", (current,))
    connection.commit()
    return {"job": row(connection.execute("SELECT * FROM jobs WHERE status='WAITING' ORDER BY created_at LIMIT 1").fetchone())}


@app.post("/api/jobs/{jid}/claim")
def claim(jid: str, data: Claim, authorization: str | None = Header(None)):
    auth(authorization); connection = db()
    cursor = connection.execute("UPDATE jobs SET status='CLAIMED',claimed_at=?,worker_id=?,lease_expires_at=?,error_message=NULL WHERE id=? AND status='WAITING'", (now().isoformat(), data.worker_id, lease_time(), jid))
    connection.commit()
    if not cursor.rowcount: raise HTTPException(409, "Job unavailable")
    job = connection.execute("SELECT timestamps_path,video_url FROM jobs WHERE id=?", (jid,)).fetchone()
    return {"id": jid, "status": "CLAIMED", "pgn_url": f"/api/jobs/{jid}/input", "timestamps_url": f"/api/jobs/{jid}/timestamps" if job[0] else None, "video_url": job[1]}


@app.get("/api/jobs/{jid}/input")
def input_file(jid: str, authorization: str | None = Header(None)):
    auth(authorization); job = db().execute("SELECT pgn_path FROM jobs WHERE id=?", (jid,)).fetchone()
    if not job: raise HTTPException(404)
    return FileResponse(job[0])


@app.get("/api/jobs/{jid}/timestamps")
def timestamps_file(jid: str, authorization: str | None = Header(None)):
    auth(authorization); job = db().execute("SELECT timestamps_path FROM jobs WHERE id=?", (jid,)).fetchone()
    if not job or not job[0]: raise HTTPException(404)
    return FileResponse(job[0], media_type="application/json")


@app.post("/api/jobs/{jid}/heartbeat")
def heartbeat(jid: str, data: WorkerAction, authorization: str | None = Header(None)):
    auth(authorization); connection = db(); owned_job(connection, jid, data.worker_id, ("CLAIMED", "PROCESSING", "UPLOADING"))
    connection.execute("UPDATE jobs SET lease_expires_at=? WHERE id=?", (lease_time(), jid)); connection.commit()
    return {"id": jid, "lease_expires_at": lease_time()}


@app.post("/api/jobs/{jid}/status")
def status(jid: str, data: Status, authorization: str | None = Header(None)):
    auth(authorization); connection = db()
    allowed = {"CLAIMED": {"PROCESSING", "FAILED"}, "PROCESSING": {"UPLOADING", "FAILED"}, "UPLOADING": {"FAILED"}}
    job = connection.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
    if not job: raise HTTPException(404, "Job not found")
    if job["worker_id"] != data.worker_id or data.status not in allowed.get(job["status"], set()): raise HTTPException(409, "Invalid job transition")
    error = (data.error_message or "")[:1000] or None
    connection.execute("UPDATE jobs SET status=?,error_message=?,started_at=CASE WHEN ?='PROCESSING' THEN ? ELSE started_at END,lease_expires_at=CASE WHEN ?='FAILED' THEN NULL ELSE ? END WHERE id=?", (data.status, error, data.status, now().isoformat(), data.status, lease_time(), jid)); connection.commit()
    return {"id": jid, "status": data.status}


@app.post("/api/jobs/{jid}/result")
def result(jid: str, worker_id: str = Form(...), result_file: UploadFile = File(...), authorization: str | None = Header(None)):
    auth(authorization); connection = db(); owned_job(connection, jid, worker_id, ("UPLOADING",))
    path = RESULTS / f"{jid}.mp4"; temporary = RESULTS / f".{jid}.upload"
    temporary.write_bytes(result_file.file.read()); temporary.replace(path)
    connection.execute("UPDATE jobs SET status='COMPLETED',completed_at=?,result_path=?,lease_expires_at=NULL WHERE id=?", (now().isoformat(), str(path), jid)); connection.commit()
    return {"id": jid, "status": "COMPLETED", "result": str(path)}


@app.post("/api/jobs/{jid}/timestamps-result")
def timestamps_result(jid: str, worker_id: str = Form(...), timestamps_file: UploadFile = File(...), authorization: str | None = Header(None)):
    auth(authorization); connection = db(); owned_job(connection, jid, worker_id, ("UPLOADING",))
    path = RESULTS / f"{jid}.timestamps.json"; data = timestamps_file.file.read(200_001)
    if len(data) > 200_000: raise HTTPException(413, "timestamps.json maximum 200 KB")
    path.write_bytes(data)
    connection.execute("UPDATE jobs SET status='COMPLETED',completed_at=?,detected_timestamps_path=?,lease_expires_at=NULL WHERE id=?", (now().isoformat(), str(path), jid)); connection.commit()
    return {"id": jid, "status": "COMPLETED"}
