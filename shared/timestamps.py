import json


def parse_timestamps(data: bytes | str, ply_count: int) -> list[dict]:
    try:
        items = json.loads(data)
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid JSON: {error}") from error
    if not isinstance(items, list) or not items:
        raise ValueError("timestamps must be a non-empty array")
    if len(items) != ply_count:
        raise ValueError(f"timestamps count {len(items)} does not match PGN plies {ply_count}")
    result, previous = [], -1.0
    for index, item in enumerate(items, 1):
        if not isinstance(item, dict) or item.get("ply") != index:
            raise ValueError(f"entry {index} must have ply {index}")
        timestamp = item.get("timestamp")
        if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)) or timestamp < 0:
            raise ValueError(f"ply {index} timestamp must be a non-negative number")
        timestamp = float(timestamp)
        if timestamp <= previous:
            raise ValueError(f"ply {index} timestamp must be greater than previous timestamp")
        result.append({"ply": index, "timestamp": timestamp})
        previous = timestamp
    return result
