import base64, importlib, json, os, sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from fastapi.testclient import TestClient
from shared.timestamps import parse_timestamps
from shared.timeline import parse_pgn
from renderer.render import render_video

ROOT = Path(__file__).parents[1]
PGN = (ROOT / "samples/grischuk-wei.pgn").read_bytes()


def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CHESS_STORAGE", str(tmp_path)); monkeypatch.setenv("WORKER_TOKEN", "token")
    monkeypatch.setenv("ADMIN_USER", "owner"); monkeypatch.setenv("ADMIN_PASSWORD", "secret")
    import controller.app as module
    importlib.reload(module)
    return TestClient(module.app), module


def admin(): return {"Authorization": "Basic " + base64.b64encode(b"owner:secret").decode()}
def worker(): return {"Authorization": "Bearer token"}
def timestamps(count): return json.dumps([{"ply": ply, "timestamp": float(ply)} for ply in range(1, count + 1)]).encode()


def test_timestamps_validation():
    assert parse_timestamps(timestamps(2), 2)[1] == {"ply": 2, "timestamp": 2.0}
    for bad in (b"{}", b'[{"ply":1,"timestamp":-1}]', b'[{"ply":2,"timestamp":1}]'):
        try: parse_timestamps(bad, 1)
        except ValueError: pass
        else: raise AssertionError("invalid timestamps accepted")


def test_dashboard_upload_and_worker_download_timestamps(tmp_path, monkeypatch):
    c, _ = client(tmp_path, monkeypatch); count = len(parse_pgn(PGN.decode())["moves"])
    response = c.post("/dashboard/jobs", headers=admin(), data={"video_url": "https://www.youtube.com/watch?v=test123"}, files={"pgn_file": ("game.pgn", PGN), "timestamps_file": ("timestamps.json", timestamps(count))}, follow_redirects=False)
    assert response.status_code == 303
    job = c.get("/api/jobs/next", headers=worker()).json()["job"]
    claim = c.post(f'/api/jobs/{job["id"]}/claim', headers=worker(), json={"worker_id": "desktop-01"}).json()
    assert claim["timestamps_url"]
    assert claim["video_url"] == "https://www.youtube.com/watch?v=test123"
    assert c.get(claim["timestamps_url"], headers=worker()).json()[0] == {"ply": 1, "timestamp": 1.0}


def test_transition_ownership_and_expired_lease_requeue(tmp_path, monkeypatch):
    c, module = client(tmp_path, monkeypatch)
    created = c.post("/api/jobs", headers=worker(), files={"pgn_file": ("game.pgn", PGN)}).json()
    jid = created["id"]
    c.post(f"/api/jobs/{jid}/claim", headers=worker(), json={"worker_id": "desktop-01"})
    denied = c.post(f"/api/jobs/{jid}/status", headers=worker(), json={"worker_id": "other", "status": "PROCESSING"})
    assert denied.status_code == 409
    connection = module.db(); connection.execute("UPDATE jobs SET lease_expires_at=? WHERE id=?", ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), jid)); connection.commit()
    assert c.get("/api/jobs/next", headers=worker()).json()["job"]["id"] == jid


def test_timestamp_video_duration(tmp_path):
    timeline = parse_pgn(PGN.decode()); output = tmp_path / "timed.mp4"
    timing = [{"ply": 1, "timestamp": .4}, {"ply": 2, "timestamp": 1.0}]
    render_video(timeline, output, size=(320, 180), fps=10, seconds_per_ply=.2, max_plies=2, encoder="libx264", timestamps=timing)
    assert output.stat().st_size > 1000
