import os
os.environ.setdefault("CONTROLLER_URL", "https://example.invalid")
os.environ.setdefault("WORKER_TOKEN", "test-token")
from pathlib import Path
from worker.worker import yt_dlp_command


def test_yt_dlp_uses_current_python_module():
    command = yt_dlp_command("https://youtu.be/test", Path("video.mp4"))
    assert command[1:3] == ["-m", "yt_dlp"]
