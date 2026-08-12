"""Per-ply evaluations for the bar, from an engine or from the PGN itself.

Two sources, because they fail in different ways. A PGN exported from Lichess or
an analysis site already carries [%eval ...] and needs nothing installed; a local
engine works for any PGN but has to be present on the machine.
"""
import logging
import re
import shutil
from pathlib import Path

import chess
import chess.engine
import chess.pgn

log = logging.getLogger(__name__)

EVAL_TAG = re.compile(r"\[%eval\s+(#?[-+]?\d+(?:\.\d+)?)\]")
CANDIDATES = ("stockfish", "stockfish.exe")
LOCAL = Path(__file__).parents[1] / "engines"


def find_engine(explicit=None):
    """An explicit path, then ./engines, then whatever is on PATH."""
    if explicit and Path(explicit).is_file():
        return Path(explicit)
    if LOCAL.is_dir():
        for path in sorted(LOCAL.rglob("*")):
            if path.is_file() and path.suffix.lower() in ("", ".exe") \
                    and "stockfish" in path.name.lower():
                return path
    for name in CANDIDATES:
        if found := shutil.which(name):
            return Path(found)
    return None


def _score(cp=None, mate=None):
    return {"cp": cp, "mate": mate}


def from_pgn(text: str, ply_count: int) -> list[dict] | None:
    """Read [%eval] comments if the PGN carries them. None when it does not."""
    game = chess.pgn.read_game(__import__("io").StringIO(text))
    if game is None:
        return None
    found, node = [], game
    while node.variations:
        node = node.variations[0]
        match = EVAL_TAG.search(node.comment or "")
        if not match:
            found.append(None)
            continue
        raw = match.group(1)
        if raw.startswith("#"):
            found.append(_score(mate=int(raw[1:])))
        else:
            found.append(_score(cp=int(round(float(raw) * 100))))
    if not any(found):
        return None
    log.info("Evaluations read from PGN: %d/%d plies", sum(x is not None for x in found), ply_count)
    return found


def default_threads():
    return max(1, (__import__("os").cpu_count() or 2) - 1)


def from_engine(timeline: dict, engine_path, movetime=0.25, threads=None, hash_mb=256):
    """Score every position with a local UCI engine, from white's point of view."""
    engine_path = Path(engine_path)
    board = chess.Board(timeline["initial_fen"])
    results = []
    with chess.engine.SimpleEngine.popen_uci(str(engine_path)) as engine:
        try:
            engine.configure({"Threads": threads or default_threads(), "Hash": hash_mb})
        except chess.engine.EngineError:
            pass
        for index, move in enumerate(timeline["moves"], 1):
            board.push(chess.Move.from_uci(move["uci"]))
            info = engine.analyse(board, chess.engine.Limit(time=movetime))
            score = info["score"].white()
            results.append(_score(mate=score.mate()) if score.is_mate()
                           else _score(cp=score.score()))
            if index % 20 == 0:
                log.info("Evaluated %d/%d plies", index, len(timeline["moves"]))
    log.info("Engine evaluation finished: %d plies", len(results))
    return results


def advantage(score: dict | None) -> float:
    """Score -> white's share of the bar, 0..1. Lichess' winning-chances curve."""
    if not score:
        return 0.5
    if score.get("mate") is not None:
        return 1.0 if score["mate"] > 0 else 0.0
    cp = max(-1500, min(1500, score.get("cp") or 0))
    import math
    return 1 / (1 + math.exp(-0.00368208 * cp))


def label(score: dict | None) -> str:
    if not score:
        return ""
    if score.get("mate") is not None:
        return "#" if score["mate"] == 0 else f"M{abs(score['mate'])}"
    cp = (score.get("cp") or 0) / 100.0
    return f"{cp:+.1f}"
