from worker.detect_timestamps import stable_end_times


def test_reports_end_after_motion_becomes_stable():
    scores = [(i / 4, 0.5) for i in range(240)]
    for start, end in ((0, 4.25), (18, 19.25), (23, 24.25), (37.75, 41.75), (51.75, 52.75)):
        scores = [(t, 5.0 if start <= t <= end else score) for t, score in scores]
    assert stable_end_times(scores, threshold=2.5, stable_seconds=1.0) == [4.5, 19.5, 24.5, 42.0, 53.0]


def test_merges_short_gap_inside_one_move():
    scores = [(0.0, 4.0), (0.25, 0.0), (0.5, 4.0), (0.75, 0.0), (1.0, 0.0)]
    assert stable_end_times(scores, threshold=2.5, stable_seconds=.5) == [0.75]


def test_rejects_one_frame_noise():
    scores = [(0.0, 0.0), (0.25, 4.0), (0.5, 0.0), (0.75, 0.0), (1.0, 0.0)]
    assert stable_end_times(scores, threshold=2.5, stable_seconds=.5) == []
