import json
from worker.ai_timestamp_detector import parse_json


def test_parse_json_ignores_hermes_session_footer():
    text = '[{"timestamp":8.75,"confidence":0.9}]\n\nsession_id: abc\n'
    assert parse_json(text) == [{"timestamp": 8.75, "confidence": .9}]


def test_parse_json_rejects_missing_array():
    try: parse_json("model failed")
    except RuntimeError as error: assert "no JSON" in str(error)
    else: raise AssertionError("missing JSON accepted")
