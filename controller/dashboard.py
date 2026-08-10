import html, os, secrets, sqlite3, uuid
from datetime import datetime, timezone
from pathlib import Path
from fastapi import Header, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from shared.timeline import parse_pgn

PAGE = '''<!doctype html><html lang="id"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Chess Video Jobs</title><style>
*{box-sizing:border-box}body{margin:0;background:#10131a;color:#eef1f7;font:16px system-ui}main{max-width:760px;margin:auto;padding:24px}h1{font-size:26px}form,.job{background:#1b202b;padding:16px;border-radius:12px;margin:14px 0}input,button,a{font:inherit}input{width:100%;padding:12px;background:#0f131b;color:#fff;border:1px solid #424b5e;border-radius:8px}button,.download{display:inline-block;margin-top:12px;padding:11px 16px;background:#58a6ff;color:#07111e;border:0;border-radius:8px;font-weight:700;text-decoration:none}.status{font-weight:700}.FAILED{color:#ff7b72}.COMPLETED{color:#56d364}.WAITING{color:#d29922}small{color:#9da7b8;word-break:break-all}</style></head><body><main><h1>Chess Video Jobs</h1><form action="/dashboard/jobs" method="post" enctype="multipart/form-data"><label>File PGN</label><input name="pgn_file" type="file" accept=".pgn" required><button>Buat Job</button></form><section id="jobs">Memuat…</section></main><script>
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));async function load(){let r=await fetch('/dashboard/jobs');if(!r.ok)return;let d=await r.json();jobs.innerHTML=d.jobs.length?d.jobs.map(j=>`<article class="job"><div class="status ${esc(j.status)}">${esc(j.status)}</div><small>${esc(j.id)}</small>${j.error_message?`<p>${esc(j.error_message)}</p>`:''}${j.status==='COMPLETED'?`<a class="download" href="/dashboard/jobs/${esc(j.id)}/result">Download MP4</a>`:''}</article>`).join(''):'Belum ada job.'}load();setInterval(load,5000)</script></body></html>'''

def admin_auth(authorization):
    import base64
    if not authorization or not authorization.startswith("Basic "):
        raise HTTPException(401, "Admin login required", headers={"WWW-Authenticate": "Basic"})
    try:
        user, password = base64.b64decode(authorization[6:]).decode().split(":", 1)
    except Exception:
        raise HTTPException(401, "Invalid admin login", headers={"WWW-Authenticate": "Basic"})
    if not (secrets.compare_digest(user, os.getenv("ADMIN_USER", "")) and secrets.compare_digest(password, os.getenv("ADMIN_PASSWORD", ""))):
        raise HTTPException(401, "Invalid admin login", headers={"WWW-Authenticate": "Basic"})

def add_routes(app, db, inputs, results):
    @app.get("/", response_class=HTMLResponse)
    def dashboard(authorization: str | None = Header(None)):
        admin_auth(authorization); return PAGE

    @app.get("/dashboard/jobs")
    def jobs(authorization: str | None = Header(None)):
        admin_auth(authorization)
        rows = db().execute("SELECT id,status,created_at,completed_at,worker_id,error_message FROM jobs ORDER BY created_at DESC LIMIT 100").fetchall()
        return {"jobs": [dict(row) for row in rows]}

    @app.post("/dashboard/jobs")
    def create_job(pgn_file: UploadFile, authorization: str | None = Header(None)):
        admin_auth(authorization); data = pgn_file.file.read(2_000_001)
        if len(data) > 2_000_000: raise HTTPException(413, "PGN maximum 2 MB")
        try: parse_pgn(data.decode("utf-8"))
        except Exception as error: raise HTTPException(400, f"Invalid PGN: {error}")
        jid=uuid.uuid4().hex; path=inputs/f"{jid}.pgn"; path.write_bytes(data); c=db(); c.execute("INSERT INTO jobs(id,status,created_at,pgn_path) VALUES(?,?,?,?)",(jid,"WAITING",datetime.now(timezone.utc).isoformat(),str(path))); c.commit()
        return RedirectResponse("/", status_code=303)

    @app.get("/dashboard/jobs/{jid}/result")
    def download(jid: str, authorization: str | None = Header(None)):
        admin_auth(authorization); row=db().execute("SELECT result_path FROM jobs WHERE id=? AND status='COMPLETED'",(jid,)).fetchone()
        if not row or not row[0] or not Path(row[0]).is_file(): raise HTTPException(404, "Result unavailable")
        return FileResponse(row[0], media_type="video/mp4", filename=f"{jid}.mp4")
