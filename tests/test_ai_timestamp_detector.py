import pytest
from worker.ai_timestamp_detector import format_prompt, parse_json


def test_parse_json_ignores_hermes_session_footer():
    text = '[{"timestamp":8.75,"confidence":0.9}]\n\nsession_id: abc\n'
    assert parse_json(text) == [{"timestamp": 8.75, "confidence": .9}]


def test_parse_json_rejects_missing_array():
    with pytest.raises(RuntimeError, match="no JSON"):
        parse_json("model failed")


def test_prompt_formats_literal_json_example():
    prompt = format_prompt("b3 c5", 0, 10)
    assert '{"timestamp":8.75,"confidence":0.9}' in prompt
    assert "0.00–10.00s" in prompt
