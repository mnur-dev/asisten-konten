# Chess Automation MVP

VPS controller + pull-based Windows renderer. Renderer path: PGN → timeline → board frames → FFmpeg MP4.

## Lightweight verification
```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt pytest httpx2
.venv/bin/pytest -q
.venv/bin/python -m renderer.cli samples/grischuk-wei.pgn artifacts/short.mp4 --width 640 --height 360 --fps 5 --seconds-per-ply .2 --max-plies 5
```

## Controller development run
```bash
export WORKER_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
.venv/bin/uvicorn controller.app:app --host 127.0.0.1 --port 8767
```
Keep bound to localhost. Put HTTPS reverse proxy in front later; no Hestia/nginx config changed by this project.

Create job:
```bash
curl -H "Authorization: Bearer $WORKER_TOKEN" -F "pgn_file=@samples/grischuk-wei.pgn" http://127.0.0.1:8767/api/jobs
```
