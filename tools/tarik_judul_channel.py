#!/usr/bin/env python3
"""Refresh title-patterns/<slug>.json: every public video's title, date, length, views.

Uses the OAuth tokens of /home/ubuntu/yt-analytics (read-only scope is enough), so run
it with that project's venv, which has the Google client libraries:

    /home/ubuntu/yt-analytics/.venv/bin/python tools/tarik_judul_channel.py pawn-initiate

The written pattern guide (<slug>.md) is NOT regenerated -- re-read the numbers and
update it by hand when the channel's patterns shift.
"""
import datetime
import json
import re
import sys
from pathlib import Path

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

TOKENS = Path.home() / ".config" / "yt-analytics"
OUT = Path(__file__).resolve().parents[1] / "title-patterns"


def seconds(duration: str) -> int:
    h, m, s = (int(x or 0) for x in re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", duration).groups())
    return h * 3600 + m * 60 + s


def main(slug: str):
    creds = Credentials.from_authorized_user_info(json.loads((TOKENS / f"token-{slug}.json").read_text()))
    yt = build("youtube", "v3", credentials=creds, cache_discovery=False)
    channel = yt.channels().list(part="snippet,contentDetails", mine=True).execute()["items"][0]
    uploads = channel["contentDetails"]["relatedPlaylists"]["uploads"]
    ids, page = [], None
    while True:
        r = yt.playlistItems().list(part="contentDetails", playlistId=uploads, maxResults=50, pageToken=page).execute()
        ids += [i["contentDetails"]["videoId"] for i in r["items"]]
        if not (page := r.get("nextPageToken")):
            break
    videos = []
    for i in range(0, len(ids), 50):
        for v in yt.videos().list(part="snippet,statistics,contentDetails,status", id=",".join(ids[i:i + 50])).execute()["items"]:
            if v["status"]["privacyStatus"] == "public":
                videos.append({"id": v["id"], "title": v["snippet"]["title"], "published": v["snippet"]["publishedAt"][:10],
                               "seconds": seconds(v["contentDetails"]["duration"]),
                               "views": int(v["statistics"].get("viewCount", 0))})
    videos.sort(key=lambda v: v["published"], reverse=True)
    OUT.mkdir(exist_ok=True)
    (OUT / f"{slug}.json").write_text(json.dumps({"channel": channel["snippet"]["title"],
        "fetched": datetime.date.today().isoformat(), "videos": videos}, ensure_ascii=False, indent=1))
    print(f"{len(videos)} video publik -> {OUT / (slug + '.json')}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "pawn-initiate")
