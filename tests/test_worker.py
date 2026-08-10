import os
os.environ.setdefault("CONTROLLER_URL", "https://example.invalid")
os.environ.setdefault("WORKER_TOKEN", "test-token")
import pytest
from worker.worker import get_next_job, nvenc_capability

class Response:
    status_code = 401
    text = '{"detail":"Invalid worker token"}'
    def raise_for_status(self):
        import requests
        raise requests.HTTPError("401 Client Error", response=self)

def test_auth_error_is_clear(monkeypatch):
    monkeypatch.setattr("worker.worker.requests.get", lambda *a, **k: Response())
    with pytest.raises(RuntimeError, match="authentication failed"):
        get_next_job()

def test_nvenc_failure_returns_ffmpeg_reason(monkeypatch):
    class Result:
        returncode = 1
        stderr = b"Cannot load nvcuda.dll"
    monkeypatch.setattr("worker.worker.shutil.which", lambda _: "ffmpeg")
    monkeypatch.setattr("worker.worker.subprocess.run", lambda *a, **k: Result())
    available, reason = nvenc_capability()
    assert available is False
    assert reason == "Cannot load nvcuda.dll"
