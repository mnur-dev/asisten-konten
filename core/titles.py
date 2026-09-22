"""Five title suggestions per video, written by Claude Code.

Three inputs are combined into one prompt: the source video's own title (its angle,
never its wording), facts pulled from the PGN (players, ratings, event, result, the
graded moments from moves.json, clocks), and the target channel's measured title
patterns (title-patterns/<channel>.md plus its best and most recent real titles from
<channel>.json). Claude Code is called headless (`claude -p`) with no tools and a
JSON schema, so the answer is structured and nothing else can happen in that call.
"""
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import chess

from core.pgn import board_from, parse_pgn

ROOT = Path(__file__).resolve().parents[1]
PATTERNS = ROOT / "title-patterns"
MODEL = os.environ.get("TITLE_MODEL", "claude-opus-5")
COUNT = 5
SHORT_MAX = 180                       # seconds: the corpus split between Shorts and long videos

SCHEMA = {
    "type": "object",
    "properties": {"titles": {"type": "array", "minItems": COUNT, "maxItems": COUNT, "items": {
        "type": "object",
        "properties": {"title": {"type": "string"},
                       "angle": {"type": "string", "description": "one short line: which pattern and which game fact it uses"}},
        "required": ["title", "angle"]}}},
    "required": ["titles"],
}


def claude_bin() -> str:
    """The Claude Code CLI. The systemd unit's PATH does not include ~/.local/bin,
    where the installer puts it, so that is checked explicitly."""
    for candidate in (os.environ.get("CLAUDE_BIN"), shutil.which("claude"),
                      str(Path.home() / ".local" / "bin" / "claude")):
        if candidate and Path(candidate).is_file():
            return candidate
    raise RuntimeError("Claude Code CLI (claude) tidak ditemukan di server")


def _pgn_headers(text: str) -> dict:
    headers = {}
    for line in text.splitlines():
        if line.startswith("[") and '"' in line:
            key, _, rest = line[1:].partition(" ")
            headers[key] = rest.strip().rstrip("]").strip('"')
    return headers


def pgn_facts(folder: Path, meta: dict) -> str:
    """Plain-language facts about the game, the only things a title may claim."""
    text = (folder / "input.pgn").read_text(encoding="utf-8")
    headers = _pgn_headers(text)
    timeline = parse_pgn(text)
    moves = timeline["moves"]
    white, black = headers.get("White", meta.get("white", "")), headers.get("Black", meta.get("black", ""))
    lines = [f"White: {white}" + (f" ({headers['WhiteElo']})" if headers.get("WhiteElo") else ""),
             f"Black: {black}" + (f" ({headers['BlackElo']})" if headers.get("BlackElo") else "")]
    for key in ("Event", "Round", "Date", "TimeControl", "ECO", "Opening"):
        if headers.get(key) and headers[key] != "?":
            lines.append(f"{key}: {headers[key]}")

    board = board_from(timeline)
    for move in moves:
        board.push_san(move["san"])
    result = headers.get("Result", "*")
    winner = white if result == "1-0" else black if result == "0-1" else None
    loser = black if winner == white else white
    how = ("by checkmate" if board.is_checkmate()
           else f"({loser} resigned or lost on time; no checkmate on the board)" if winner else "")
    lines.append(f"Result: {result}" + (f" — {winner} won {how}" if winner else " — draw" if result == "1/2-1/2" else ""))
    try:
        elo_w, elo_b = int(headers["WhiteElo"]), int(headers["BlackElo"])
        gap = abs(elo_w - elo_b)
        if gap >= 50 and winner:
            upset = (winner == white) == (elo_w < elo_b)
            lines.append(f"Rating gap: {gap} points — the {'LOWER' if upset else 'higher'}-rated player won"
                         + (" (an upset)" if upset else ""))
    except (KeyError, ValueError):
        pass
    lines.append(f"Length: {len(moves)} plies ({(len(moves) + 1) // 2} moves)")

    grades_file = folder / "moves.json"
    if grades_file.is_file():
        notable = []
        for grade in json.loads(grades_file.read_text(encoding="utf-8")):
            if grade and grade.get("label") in ("brilliant", "great", "blunder", "mistake"):
                number = (grade["ply"] + 1) // 2
                who = white if grade["side"] == "white" else black
                dots = "." if grade["side"] == "white" else "..."
                extra = f", sacrificing {grade['sacrifice']:g} points of material" if grade.get("sacrifice") else ""
                notable.append(f"- {number}{dots}{grade['san']} by {who}: {grade['label']}{extra}")
        lines.append("Engine-graded key moments:" + ("\n" + "\n".join(notable) if notable else " none beyond inaccuracies — a clean game"))

    clocks = [m.get("clock") for m in moves if m.get("clock") is not None]
    if clocks:
        low = min(clocks)
        if low < 30:
            lines.append(f"Time trouble: a clock fell to {low:.0f} seconds")
    return "\n".join(lines)


def channel_patterns(slug: str, kind: str) -> str:
    """The written pattern guide plus real titles: the best of this kind (what works)
    and the most recent ones (what must not be repeated)."""
    guide = (PATTERNS / f"{slug}.md").read_text(encoding="utf-8")
    corpus = json.loads((PATTERNS / f"{slug}.json").read_text(encoding="utf-8"))["videos"]
    same = [v for v in corpus if (v["seconds"] <= SHORT_MAX) == (kind == "short")]
    best = sorted(same, key=lambda v: -v["views"])[:25]
    recent = sorted(corpus, key=lambda v: v["published"], reverse=True)[:40]
    fmt = lambda v: f"- {v['title']}  ({v['views']:,} views)"
    return (f"{guide}\n\n## Best {'Shorts' if kind == 'short' else 'long videos'} on the channel\n"
            + "\n".join(map(fmt, best))
            + "\n\n## Most recent titles (do not repeat these)\n" + "\n".join(f"- {v['title']}" for v in recent))


def suggest(folder: Path, meta: dict, kind: str, slug: str = "pawn-initiate") -> list[dict]:
    source = meta.get("source_title") or "(unknown)"
    prompt = f"""Write {COUNT} YouTube titles for a {'Short (vertical, under a minute)' if kind == 'short' else 'long video (the full game, several minutes)'}
on the chess channel {slug.replace('-', ' ').title()}.

# 1. The source video
The footage comes from a broadcast titled: "{source}"{f' (channel: {meta["source_channel"]})' if meta.get("source_channel") else ''}.
Use it for the angle only; never reuse its wording.

# 2. Facts from the PGN — the only claims a title may make
{pgn_facts(folder, meta)}

# 3. What works on this channel
{channel_patterns(slug, kind)}

Combine all three: each title must rest on a real fact of this game and follow a pattern
that performs on this channel. Make the {COUNT} titles use different patterns from each other."""
    system = ("You are a YouTube title writer for a chess channel. Titles are in English. "
              "Answer only with the structured output: exactly five titles, each with a one-line "
              "angle written in Bahasa Indonesia (the channel owner reads Indonesian).")
    with tempfile.TemporaryDirectory() as scratch:     # keeps repo/user CLAUDE.md out of the call
        out = subprocess.run(
            [claude_bin(), "-p", "--output-format", "json", "--tools", "", "--model", MODEL,
             "--no-session-persistence", "--system-prompt", system,
             "--json-schema", json.dumps(SCHEMA)],
            input=prompt, capture_output=True, text=True, timeout=300, cwd=scratch)
    if out.returncode != 0:
        raise RuntimeError(f"Claude Code gagal: {(out.stderr or out.stdout).strip()[-300:]}")
    reply = json.loads(out.stdout)
    if reply.get("is_error"):
        raise RuntimeError(f"Claude Code gagal: {str(reply.get('result'))[-300:]}")
    titles = (reply.get("structured_output") or json.loads(reply["result"]))["titles"]
    return [{"title": t["title"].strip(), "angle": t.get("angle", "").strip()} for t in titles][:COUNT]
