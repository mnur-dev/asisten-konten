import io
import chess.pgn

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
        moves.append({"ply": ply, "move_number": (ply + 1) // 2, "side": "white" if ply % 2 else "black", "san": san, "uci": move.uci(), "fen_before": before, "fen_after": board.fen(), "timestamp": float(ply - 1)})
    return {"headers": dict(game.headers), "initial_fen": game.board().fen(), "moves": moves}
