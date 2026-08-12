"""PGN -> move timeline plus the per-ply occupancy signatures used for matching."""
import io
import chess
import chess.pgn
import numpy as np


def parse_pgn(text: str) -> dict:
    game = chess.pgn.read_game(io.StringIO(text))
    if game is None:
        raise ValueError("PGN contains no game")
    if game.errors:
        raise ValueError(f"Invalid PGN: {game.errors[0]}")
    board = game.board()
    moves = []
    for ply, move in enumerate(game.mainline_moves(), 1):
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
            "fen_before": before,
            "fen_after": board.fen(),
        })
    return {"headers": dict(game.headers), "initial_fen": game.board().fen(), "moves": moves}


def signature(board: chess.Board) -> np.ndarray:
    """8x8 of {0 empty, +1 white piece, -1 black piece}, row 0 = rank 8."""
    out = np.zeros((8, 8), np.int8)
    for square, piece in board.piece_map().items():
        out[7 - chess.square_rank(square), chess.square_file(square)] = 1 if piece.color else -1
    return out


def signatures(timeline: dict) -> np.ndarray:
    """(plies+1, 8, 8) — index n is the position after n plies."""
    board = chess.Board(timeline["initial_fen"])
    out = [signature(board)]
    for move in timeline["moves"]:
        board.push(chess.Move.from_uci(move["uci"]))
        out.append(signature(board))
    return np.array(out)
