"""PGN -> move timeline plus the per-ply occupancy signatures used for matching."""
import io
import re

import chess
import chess.pgn
import numpy as np

# [%clk 0:29:57] as Lichess/chess.com write it, plus the bare mm:ss some tools emit
CLOCK_TAG = re.compile(r"\[%clk\s+(?:(\d+):)?(\d{1,2}):(\d{1,2}(?:\.\d+)?)\]")


def parse_clock(comment: str):
    """Seconds left on the mover's clock, from a [%clk] comment. None when absent."""
    match = CLOCK_TAG.search(comment or "")
    if not match:
        return None
    hours, minutes, seconds = match.groups()
    return int(hours or 0) * 3600 + int(minutes) * 60 + float(seconds)


def parse_pgn(text: str) -> dict:
    game = chess.pgn.read_game(io.StringIO(text))
    if game is None:
        raise ValueError("PGN contains no game")
    if game.errors:
        raise ValueError(f"Invalid PGN: {game.errors[0]}")
    board = game.board()
    moves = []
    node = game
    ply = 0
    # walked node by node rather than through mainline_moves(), which drops the
    # comments the clock times live in
    while node.variations:
        node = node.variations[0]
        move = node.move
        ply += 1
        san = board.san(move)
        before = board.fen()
        board.push(move)
        moves.append({
            "ply": ply,
            "move_number": (ply + 1) // 2,
            "side": "white" if ply % 2 else "black",
            "san": san,
            "uci": move.uci(),
            "from": chess.square_name(move.from_square),
            "to": chess.square_name(move.to_square),
            "clock": parse_clock(node.comment),
            "fen_before": before,
            "fen_after": board.fen(),
        })
    return {"headers": dict(game.headers), "initial_fen": game.board().fen(),
            "chess960": game.board().chess960, "moves": moves}


def board_from(timeline: dict, fen: str | None = None) -> chess.Board:
    """Rebuild a board from a timeline, keeping its variant.

    The chess960 flag is not cosmetic: in Freestyle/960 a castling move is written
    king-takes-rook (d1g1), and a board that does not know it is a 960 game hands the
    engine a position it cannot parse -- Stockfish then answers with illegal moves and
    finally stops answering at all. Always go through here instead of chess.Board().
    """
    return chess.Board(fen or timeline["initial_fen"],
                       chess960=bool(timeline.get("chess960")))


def has_clocks(timeline: dict) -> bool:
    return any(move["clock"] is not None for move in timeline["moves"])


def initial_seconds(time_control: str):
    """Main thinking time from a TimeControl header: "5400+30", "600", "40/7200:1800".
    None for "-", "?" or anything unparseable."""
    field = (time_control or "").split(":")[0]        # first phase of a staged control
    field = field.split("/")[-1]                      # drop the "40/" move count
    field = field.split("+")[0]                       # drop the increment
    return float(field) if field.strip().isdigit() else None


def clock_series(timeline: dict) -> list[tuple]:
    """Per-ply (white, black) seconds remaining; index n is the position after n plies.

    A [%clk] comment states the time left for the side that just moved, so the other
    side keeps showing whatever its own last move left it with. Before either side has
    moved the display falls back to the TimeControl header, and failing that to each
    side's first reading -- one move's thinking time off, but far better than blank.
    """
    if not has_clocks(timeline):
        return []
    start = initial_seconds(timeline["headers"].get("TimeControl"))
    first = {}
    for move in timeline["moves"]:
        if move["clock"] is not None:
            first.setdefault(move["side"], move["clock"])
    current = {side: start if start is not None else first.get(side)
               for side in ("white", "black")}
    series = [(current["white"], current["black"])]
    for move in timeline["moves"]:
        if move["clock"] is not None:
            current[move["side"]] = move["clock"]
        series.append((current["white"], current["black"]))
    return series


def signature(board: chess.Board) -> np.ndarray:
    """8x8 of {0 empty, +1 white piece, -1 black piece}, row 0 = rank 8."""
    out = np.zeros((8, 8), np.int8)
    for square, piece in board.piece_map().items():
        out[7 - chess.square_rank(square), chess.square_file(square)] = 1 if piece.color else -1
    return out


def signatures(timeline: dict) -> np.ndarray:
    """(plies+1, 8, 8) — index n is the position after n plies."""
    board = board_from(timeline)
    out = [signature(board)]
    for move in timeline["moves"]:
        board.push(chess.Move.from_uci(move["uci"]))
        out.append(signature(board))
    return np.array(out)
