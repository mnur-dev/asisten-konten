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

from core.pgn import board_from

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


def _white(score) -> dict:
    """A python-chess PovScore as the {cp, mate} pair stored on disk, white's view."""
    white = score.white()
    if not white.is_mate():
        return _score(cp=white.score())
    plies = white.mate()
    # Mate(0) -- the side to move is mated -- and MateGiven both report zero plies,
    # so a stored 0 cannot say who won and advantage() would paint the bar for the
    # mated side. Take the sign from the centipawn equivalent instead.
    if plies == 0:
        plies = 1 if white.score(mate_score=30000) > 0 else -1
    return _score(mate=plies)


def _finished(board) -> dict:
    """Score of a position with no moves left, so the engine is never asked to
    search one -- Stockfish answers `bestmove (none)` and the MultiPV list is empty."""
    outcome = board.outcome()
    if outcome and outcome.winner is not None:
        return _score(mate=1 if outcome.winner == chess.WHITE else -1)
    return _score(cp=0)


def analyse_game(timeline: dict, engine_path, movetime=0.25, threads=None, hash_mb=256,
                 multipv=2):
    """Analyse every position of the game with a local UCI engine.

    One record per position, index n being the position after n plies -- so index 0
    is the start, which nothing else needs but the classifier does: the grade for
    move 1 compares what White got against what was on offer before he moved.

    Each record carries the white-point-of-view score, the engine's own best move
    there, and the runner-up line's score. The gap between the two is what separates
    an only-move from a comfortable choice, and it costs one extra PV rather than a
    second pass over the game.
    """
    engine_path = Path(engine_path)
    # python-chess sets UCI_Chess960 itself from the board -- it refuses the option
    # being set by hand -- so a 960 board is all that is needed here
    board = board_from(timeline)
    records = []

    def look(board):
        if board.is_game_over():
            return {**_finished(board), "best_uci": None, "second": None}
        lines = engine.analyse(board, chess.engine.Limit(time=movetime), multipv=multipv)
        if not lines:
            return {**_score(cp=0), "best_uci": None, "second": None}
        best, *rest = lines
        pv = best.get("pv") or []
        return {**_white(best["score"]),
                "best_uci": pv[0].uci() if pv else None,
                "second": _white(rest[0]["score"]) if rest else None}

    with chess.engine.SimpleEngine.popen_uci(str(engine_path)) as engine:
        try:
            engine.configure({"Threads": threads or default_threads(), "Hash": hash_mb})
        except chess.engine.EngineError:
            pass
        records.append(look(board))
        for index, move in enumerate(timeline["moves"], 1):
            board.push(chess.Move.from_uci(move["uci"]))
            records.append(look(board))
            if index % 20 == 0:
                log.info("Evaluated %d/%d plies", index, len(timeline["moves"]))
    log.info("Engine evaluation finished: %d plies", len(records) - 1)
    return records


def scores_from(records: list[dict]) -> list[dict]:
    """The eval bar's own list -- one {cp, mate} per ply -- out of analyse_game()."""
    return [{"cp": r["cp"], "mate": r["mate"]} for r in records[1:]]


def from_engine(timeline: dict, engine_path, **kwargs):
    """Per-ply scores only, for callers that just want the bar filled in."""
    return scores_from(analyse_game(timeline, engine_path, **kwargs))


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
