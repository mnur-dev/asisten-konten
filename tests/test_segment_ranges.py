import os
os.environ.setdefault("CONTROLLER_URL", "https://example.invalid")
os.environ.setdefault("WORKER_TOKEN", "test-token")
from worker.worker import segment_ranges


def test_segment_ranges_never_produce_negative_duration():
    segments = segment_ranges(565.434, 10)
    assert segments[-1] == (560, 565.434)
    assert all(end > start for start, end in segments)
    assert len(segments) == 57
