import importlib
from fastapi.testclient import TestClient

def test_job_lifecycle(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKER_TOKEN", "test-token")
    monkeypatch.setenv("CHESS_STORAGE", str(tmp_path))
    import controller.app as module
    importlib.reload(module)
    client=TestClient(module.app)
    pgn=(__import__("pathlib").Path(__file__).parents[1]/"samples/grischuk-wei.pgn").read_bytes()
    created=client.post("/api/jobs",headers={"Authorization":"Bearer test-token"},files={"pgn_file":("game.pgn",pgn)}); assert created.status_code==201
    job=client.get("/api/jobs/next",headers={"Authorization":"Bearer test-token"}).json()["job"]; assert job["status"]=="WAITING"
    claimed=client.post(f"/api/jobs/{job['id']}/claim",headers={"Authorization":"Bearer test-token"},json={"worker_id":"desktop-01"}); assert claimed.status_code==200
    download=client.get(claimed.json()["pgn_url"],headers={"Authorization":"Bearer test-token"}); assert download.content==pgn
    result=client.post(f"/api/jobs/{job['id']}/result",headers={"Authorization":"Bearer test-token"},files={"result_file":("game.mp4",b"video")}); assert result.json()["status"]=="COMPLETED"
