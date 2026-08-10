import os, sqlite3, uuid
from datetime import datetime, timezone
from pathlib import Path
from fastapi import FastAPI, Header, HTTPException, UploadFile, File
from fastapi.responses import FileResponse
from pydantic import BaseModel

app=FastAPI(title="Chess Automation Controller")
ROOT=Path(os.getenv("CHESS_STORAGE",Path(__file__).parents[1]/"storage")); INPUTS=ROOT/"inputs"; RESULTS=ROOT/"results"; DB=ROOT/"jobs.db"
for p in (INPUTS,RESULTS): p.mkdir(parents=True,exist_ok=True)
def auth(authorization):
    expected=os.getenv("WORKER_TOKEN")
    if not expected or authorization != f"Bearer {expected}": raise HTTPException(401,"Invalid worker token")
def db():
    c=sqlite3.connect(DB); c.row_factory=sqlite3.Row; c.execute("CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,status TEXT,created_at TEXT,claimed_at TEXT,started_at TEXT,completed_at TEXT,worker_id TEXT,error_message TEXT,pgn_path TEXT,result_path TEXT)"); return c
def row(j): return dict(j) if j else None
class Claim(BaseModel): worker_id:str
class Status(BaseModel): status:str; error_message:str|None=None
@app.get("/health")
def health():
    return {"status": "ok"}
@app.post("/api/jobs",status_code=201)
def create(pgn_file:UploadFile=File(...),authorization:str|None=Header(None)):
    auth(authorization); jid=uuid.uuid4().hex; path=INPUTS/f"{jid}.pgn"; path.write_bytes(pgn_file.file.read()); now=datetime.now(timezone.utc).isoformat(); c=db(); c.execute("INSERT INTO jobs(id,status,created_at,pgn_path) VALUES(?,?,?,?)",(jid,"WAITING",now,str(path))); c.commit(); return {"id":jid,"status":"WAITING"}
@app.get("/api/jobs/next")
def next_job(authorization:str|None=Header(None)):
    auth(authorization); j=db().execute("SELECT * FROM jobs WHERE status='WAITING' ORDER BY created_at LIMIT 1").fetchone(); return {"job":row(j)}
@app.post("/api/jobs/{jid}/claim")
def claim(jid:str,data:Claim,authorization:str|None=Header(None)):
    auth(authorization); c=db(); now=datetime.now(timezone.utc).isoformat(); cur=c.execute("UPDATE jobs SET status='CLAIMED',claimed_at=?,worker_id=? WHERE id=? AND status='WAITING'",(now,data.worker_id,jid)); c.commit();
    if not cur.rowcount: raise HTTPException(409,"Job unavailable")
    return {"id":jid,"status":"CLAIMED","pgn_url":f"/api/jobs/{jid}/input"}
@app.get("/api/jobs/{jid}/input")
def input_file(jid:str,authorization:str|None=Header(None)):
    auth(authorization); j=db().execute("SELECT pgn_path FROM jobs WHERE id=?",(jid,)).fetchone()
    if not j:
        raise HTTPException(404)
    return FileResponse(j[0])
@app.post("/api/jobs/{jid}/status")
def status(jid:str,data:Status,authorization:str|None=Header(None)):
    auth(authorization); c=db(); c.execute("UPDATE jobs SET status=?,error_message=?,started_at=CASE WHEN ?='PROCESSING' THEN ? ELSE started_at END WHERE id=?",(data.status,data.error_message,data.status,datetime.now(timezone.utc).isoformat(),jid)); c.commit(); return {"id":jid,"status":data.status}
@app.post("/api/jobs/{jid}/result")
def result(jid:str,result_file:UploadFile=File(...),authorization:str|None=Header(None)):
    auth(authorization); path=RESULTS/f"{jid}.mp4"; path.write_bytes(result_file.file.read()); c=db(); c.execute("UPDATE jobs SET status='COMPLETED',completed_at=?,result_path=? WHERE id=?",(datetime.now(timezone.utc).isoformat(),str(path),jid)); c.commit(); return {"id":jid,"status":"COMPLETED","result":str(path)}
