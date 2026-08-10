from urllib.parse import urlparse


def validate_video_url(value: str) -> str:
    value = value.strip()
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("URL must use HTTPS")
    host = parsed.hostname.lower()
    if host not in {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}:
        raise ValueError("only YouTube URLs are supported")
    if host == "youtu.be" and not parsed.path.strip("/"):
        raise ValueError("missing YouTube video ID")
    if host != "youtu.be" and parsed.path != "/watch":
        raise ValueError("use a YouTube watch URL")
    return value
