from pathlib import Path
from PIL import Image
from worker.ai_timestamp_detector import contact_sheet


def test_contact_sheet_crops_physical_board_and_labels_frames(tmp_path, monkeypatch):
    commands = []
    def run(command, **kwargs):
        commands.append(command)
        frames = Path(command[-1]).parent
        for index in range(40): Image.new("RGB", (640, 360), "white").save(frames / f"{index + 1:04d}.jpg")
    monkeypatch.setattr("worker.ai_timestamp_detector.subprocess.run", run)
    output = tmp_path / "sheet.jpg"
    contact_sheet("video.mp4", output, 10, 10)
    assert output.is_file()
    assert Image.open(output).size == (1600, 1440)
    assert "crop=640:360:320:340,scale=320:180" in commands[0][commands[0].index("-vf") + 1]


def test_contact_sheet_real_fixture(tmp_path):
    video = Path("/home/ubuntu/.hermes/profiles/asisten-konten/cache/videos/video_66b9a4628ed1.mp4")
    if not video.is_file(): return
    output = tmp_path / "sheet.jpg"; contact_sheet(video, output, 0, 10)
    assert Image.open(output).size == (1600, 1440)
