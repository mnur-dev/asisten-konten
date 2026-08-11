from pathlib import Path
from PIL import Image
from worker.ai_timestamp_detector import contact_sheet


def test_contact_sheet_works_without_ffmpeg_drawtext(tmp_path, monkeypatch):
    commands = []
    def run(command, **kwargs):
        commands.append(command)
        frames = Path(command[-1]).parent
        for index in range(40): Image.new("RGB", (320, 180), "white").save(frames / f"{index + 1:04d}.jpg")
    monkeypatch.setattr("worker.ai_timestamp_detector.subprocess.run", run)
    output = tmp_path / "sheet.jpg"
    contact_sheet("video.mp4", output, 10, 10)
    assert output.is_file()
    assert Image.open(output).size == (1600, 1440)
    assert all("drawtext" not in part for part in commands[0])
