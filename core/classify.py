"""Move classification: Brilliant / Blunder and the grades in between.

The eval bar says who is winning; it never says whether the move that just landed
deserves a badge. Both answers come out of the same per-position scores, but a badge
needs two things the bar does not ask for: how much winning chance the move threw
away, and whether it gave material up on purpose.

Grades are measured on Lichess' win-percentage curve, not on raw centipawns -- a
100 cp slip is nothing at +9 and decisive at 0.00, so a centipawn ladder would call
the same move a blunder in one position and a yawn in another. The three published
Lichess cut-offs (10 / 20 / 30 points of win% given away) mark inaccuracy, mistake
and blunder; the finer grades above them are ours. chess.com's own thresholds are
not published, so nothing here claims to reproduce them exactly -- the names are
borrowed, the numbers are stated below and can be tuned.

"Brilliant" is the only grade that needs the board rather than the scores: it is a
sacrifice that survives engine scrutiny. Material given up is counted with a static
exchange evaluation (see `sacrifice_value`), so an even trade and a piece won back
next move both score zero.
"""
import logging

import chess

from core.evaluation import advantage
from core.pgn import board_from

log = logging.getLogger(__name__)

# The bishop sits slightly above the knight so that giving up the exchange
# (rook 5.0 for bishop 3.25 = 1.75) clears SACRIFICE while any minor-piece trade
# stays well under it.
VALUES = {chess.PAWN: 1.0, chess.KNIGHT: 3.0, chess.BISHOP: 3.25,
          chess.ROOK: 5.0, chess.QUEEN: 9.0, chess.KING: 0.0}

# Win% given away by the move, against the engine's best line from the same position.
# Read as "loss below this rung earns this name"; worse than the last rung is a blunder.
#
# Blunder used to sit at Lichess's published 30, which in practice never fired on this
# channel's material: across 283 graded plies from four elite games the worst move lost
# 29.8 points (Nxd3, Carlsen-Keymer) and was called a mistake. The user asked for
# blunders to be easier to earn, so the rung moved to 20 and the ones below it were
# tightened to keep four usable bands. Measured on the same 283 plies: 2 blunders
# (29.8 and 21.7), 5 inaccuracies, the rest good/excellent -- still rare, no longer never.
LADDER = ((2.0, "excellent"), (8.0, "good"), (14.0, "inaccuracy"), (20.0, "mistake"))

SACRIFICE = 1.5     # pawns handed over before a move counts as a sacrifice at all
ONLY_MOVE = 15.0    # win% the second-best line must trail by for a "great" move
# About +6.0. Set at 75 (+3.0) first and that was too tight against the reference:
# chess.com calls 37...Rxe5 in Aravindh-Dubov brilliant, and Black was already +4.1
# there. What this still refuses is the sacrifice played from a position that is over
# anyway -- Bd4+ at +10.
WON = 90.0
NOT_LOSING = 40.0   # about -1: a sacrifice that leaves the player lost is not brilliant
# A sacrifice is where a shallow eval is least trustworthy -- a broadcast PGN often
# scores a sound one a few points below the engine move -- so brilliance is allowed a
# wider miss than "excellent" is. Measured: the two sacrifices this admits across the
# sample projects (Nxe4 at 3.1, Rd5 at 3.5) are both real ones.
BRILLIANT_LOSS = 4.0

# The start position, for PGNs that carry [%eval] on moves but say nothing about the
# position before move 1. Roughly what every engine reports for it.
OPENING_SCORE = {"cp": 20, "mate": None}

ORDER = ("brilliant", "great", "best", "excellent", "good",
         "inaccuracy", "mistake", "blunder")


def win_percent(score: dict | None, colour: bool) -> float:
    """0..100 chance of winning for `colour` (chess.WHITE / chess.BLACK)."""
    share = advantage(score)
    return 100.0 * (share if colour == chess.WHITE else 1.0 - share)


def _gain(board: chess.Board, square: int) -> float:
    """Material the side to move can win on `square`, in pawns, never negative.

    The swap-off half of a static exchange evaluation: take with whichever piece
    leaves the best result, recurse into the recapture, and stop the moment
    capturing stops paying. Played out on real board copies rather than on a list of
    attackers, so a battery behind the first attacker uncovers itself for free, and
    a pinned defender is never counted as one.
    """
    best = 0.0
    for move in board.legal_moves:
        if move.to_square != square:
            continue
        target = board.piece_type_at(square)
        if target is None:                       # nothing left there to take
            continue
        nxt = board.copy(stack=False)
        nxt.push(move)
        best = max(best, VALUES[target] - _gain(nxt, square))
    return best


def see(board: chess.Board, move: chess.Move) -> float:
    """Static exchange evaluation of `move`, in pawns, for the side making it.

    Negative means the move loses material once the opponent takes what is on offer:
    a bishop stepping onto a defended square scores about -3.25, while Rxh7 answered
    by Kxh7 scores the pawn it took minus the rook it left behind.
    """
    captured = board.piece_type_at(move.to_square)
    won = VALUES[captured] if captured else 0.0
    if board.is_en_passant(move):
        won = VALUES[chess.PAWN]
    if move.promotion:
        won += VALUES[move.promotion] - VALUES[chess.PAWN]
    after = board.copy(stack=False)
    after.push(move)
    return won - _gain(after, move.to_square)


def _loose_material(board: chess.Board, exclude: int | None = None) -> float:
    """Most material the side to move can win with a single capture, `exclude`d
    square aside. 0 when nothing on the board is worth taking."""
    best = 0.0
    for move in board.legal_moves:
        if move.to_square == exclude or not board.is_capture(move):
            continue
        best = max(best, see(board, move))
    return best


def sacrifice_value(board: chess.Board, move: chess.Move) -> float:
    """Pawns `move` hands over, counting the piece it puts en prise and anything
    else it leaves hanging. 0 when the move wins material or trades evenly.

    Two sources, because the sacrifice is not always the moving piece: a queen that
    steps away from the defence of a rook gives up the rook without ever being
    attacked herself. The moved piece is measured by its own exchange (so a capture
    is credited with what it took), every other square by what the opponent can win
    there in reply.

    That second source is only a sacrifice if the move *created* it. A knight that
    has been standing en prise for three moves -- taboo because taking it drops a
    piece to a fork -- is still capturable after any quiet move, and without this
    baseline every one of those quiet moves reads as a fresh piece sacrifice.
    Measured on a null move, so "what could he already win here" and "what can he
    win now" are the same question asked twice.
    """
    after = board.copy(stack=False)
    after.push(move)
    idle = board.copy(stack=False)
    idle.turn = not idle.turn
    exposed = _loose_material(after, exclude=move.to_square) \
        - _loose_material(idle, exclude=move.to_square)
    return max(0.0, -see(board, move), exposed)


def _grade(board: chess.Board, move: chess.Move, before: dict, after: dict):
    """(label, win% lost, pawns sacrificed) for one move, board still un-pushed."""
    mover = board.turn
    available = win_percent(before, mover)       # what the engine's own move was worth
    got = win_percent(after, mover)
    loss = max(0.0, available - got)
    played_best = before.get("best_uci") == move.uci()

    # only worth the exchange arithmetic for moves that cost next to nothing -- a
    # sacrifice the engine dislikes is just a blunder with extra steps
    sacrificed = sacrifice_value(board, move) if loss <= BRILLIANT_LOSS else 0.0
    if (sacrificed >= SACRIFICE and got >= NOT_LOSING and available <= WON
            and board.legal_moves.count() > 1):
        return "brilliant", loss, sacrificed

    # A recapture is usually the only move that does not simply drop the piece back,
    # so the gap to the runner-up is wide for a reason nobody would call great.
    recapture = bool(board.move_stack) and move.to_square == board.peek().to_square
    second = before.get("second")
    if played_best and second is not None and not recapture \
            and board.legal_moves.count() > 1 \
            and available - win_percent(second, mover) >= ONLY_MOVE:
        return "great", loss, sacrificed
    if played_best:
        return "best", loss, sacrificed
    for limit, name in LADDER:
        if loss < limit:
            return name, loss, sacrificed
    return "blunder", loss, sacrificed


def classify_game(timeline: dict, positions: list) -> list[dict]:
    """One record per ply, in ply order.

    `positions[n]` is the analysis of the position after n plies from white's point
    of view -- so index 0 is the start position, which the engine path evaluates and
    the [%eval] path fills in with OPENING_SCORE. A {"cp", "mate"} dict is the
    minimum; with "best_uci" and "second" (the engine's own move and its runner-up
    score) the "best" and "great" grades become available too. A None entry -- a PGN
    that comments only some of its moves -- leaves that ply unlabelled rather than
    guessing from a score that is not there.
    """
    board = board_from(timeline)
    out = []
    for index, move in enumerate(timeline["moves"], 1):
        before = positions[index - 1] if index - 1 < len(positions) else None
        after = positions[index] if index < len(positions) else None
        played = chess.Move.from_uci(move["uci"])
        record = {"ply": move["ply"], "side": move["side"], "san": move["san"],
                  "label": None, "loss": None, "sacrifice": 0.0}
        if before and after:
            label, loss, sacrificed = _grade(board, played, before, after)
            record.update(label=label, loss=round(loss, 1),
                          sacrifice=round(sacrificed, 2))
        board.push(played)
        out.append(record)
    counts = summary(out)
    log.info("Classified %d plies (putih %s / hitam %s)", len(out),
             phrase(counts["white"]), phrase(counts["black"]))
    return out


def summary(records: list[dict]) -> dict:
    """Per-side label counts -- the line a thumbnail or a caption wants."""
    out = {"white": {}, "black": {}}
    for record in records:
        if record.get("label"):
            side = out[record["side"]]
            side[record["label"]] = side.get(record["label"], 0) + 1
    return out


def phrase(counts: dict) -> str:
    return ", ".join(f"{counts[name]} {name}" for name in ORDER if counts.get(name)) or "-"
