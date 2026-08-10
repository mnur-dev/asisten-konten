import base64, importlib
from pathlib import Path
from fastapi.testclient import TestClient


def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CHESS_STORAGE", str(tmp_path))
    monkeypatch.setenv("ADMIN_USER", "owner")
    monkeypatch.setenv("ADMIN_PASSWORD", "secret")
    import controller.app as module
    importlib.reload(module)
    return TestClient(module.app)


def basic():
    return {"Authorization": "Basic " + base64.b64encode(b"owner:secret").decode()}


def test_root_requires_admin_login(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    assert c.get("/").status_code == 401
    response = c.get("/", headers=basic())
    assert response.status_code == 200
    assert "Chess Video Jobs" in response.text


def test_dashboard_creates_and_lists_job(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    pgn = (Path(__file__).parents[1] / "samples/grischuk-wei.pgn").read_bytes()
    created = c.post("/dashboard/jobs", headers=basic(), data={"video_url": "https://youtu.be/test123"}, files={"pgn_file": ("game.pgn", pgn)}, follow_redirects=False)
    assert created.status_code == 303
    jobs = c.get("/dashboard/jobs", headers=basic()).json()["jobs"]
    assert len(jobs) == 1
    assert jobs[0]["status"] == "WAITING"


def test_dashboard_rejects_text_without_moves(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    response = c.post("/dashboard/jobs", headers=basic(), data={"video_url": "https://youtu.be/test123"}, files={"pgn_file": ("bad.pgn", b"not a chess game")})
    assert response.status_code == 400


def test_dashboard_rejects_non_youtube_url(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    pgn = (Path(__file__).parents[1] / "samples/grischuk-wei.pgn").read_bytes()
    response = c.post("/dashboard/jobs", headers=basic(), data={"video_url": "https://example.com/video"}, files={"pgn_file": ("game.pgn", pgn)})
    assert response.status_code == 400
