import os
os.environ.setdefault("CONTROLLER_URL", "https://example.invalid")
os.environ.setdefault("WORKER_TOKEN", "test-token")
import pytest
from worker.worker import get_next_job

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
