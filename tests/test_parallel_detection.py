import os
os.environ.setdefault("CONTROLLER_URL", "https://example.invalid")
os.environ.setdefault("WORKER_TOKEN", "test-token")
import threading, time
from pathlib import Path
from worker.worker import analyze_sheets_parallel


def test_analyzes_sheets_concurrently_and_orders_results(tmp_path):
    active = peak = 0; lock = threading.Lock()
    sheets = []
    for start in (0, 10, 20):
        path = tmp_path / f"{start}.jpg"; path.write_bytes(b"image"); sheets.append((start, start + 10, path))
    def analyze(start, end, sheet, moves):
        nonlocal active, peak
        with lock: active += 1; peak = max(peak, active)
        time.sleep(.03)
        with lock: active -= 1
        return [{"timestamp": start + 5, "confidence": .9}]
    results = analyze_sheets_parallel(sheets, ["b3"] * 20, analyze, workers=3)
    assert peak == 3
    assert [item["timestamp"] for item in results] == [5, 15, 25]


def test_deduplicates_boundary_timestamp(tmp_path):
    sheets = [(0, 10, tmp_path / "a"), (10, 20, tmp_path / "b")]
    def analyze(start, end, sheet, moves): return [{"timestamp": 10, "confidence": .8}]
    assert len(analyze_sheets_parallel(sheets, [], analyze, workers=2)) == 1
