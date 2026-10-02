"""Matchup helpers shared by the HR and strikeout models: handedness, workload, lineup exposure."""

from __future__ import annotations

from mlb_prop_predictor.domain import Counts, Player
from mlb_prop_predictor.models.common import clamp, regress

# Average plate appearances per game by batting-order slot (MLB, recent seasons).
SLOT_PA = (4.65, 4.55, 4.45, 4.35, 4.25, 4.13, 4.01, 3.89, 3.77)
UNKNOWN_SLOT_PA = 4.1

# Starter workload prior: batters faced per start.
LEAGUE_BF_PER_START = 22.0
BF_PRIOR_STARTS = 3
OPENER_BF = 9.0


def batter_hand_vs(batter: Player, pitcher_hand: str | None) -> str:
    """Side the batter actually hits from against this pitcher (switch hitters take the opposite side)."""
    side = batter.bat_side or "R"
    if side == "S":
        return "L" if (pitcher_hand or "R") == "R" else "R"
    return side


def platoon_ratio(
    split: Counts | None, overall_rate: float, attr: str, prior_n: float, low: float = 0.7, high: float = 1.3
) -> float:
    """How much better/worse a player is in one platoon split than overall, heavily regressed.

    The split rate is shrunk toward the player's *own* overall rate, so small samples ~ 1.0.
    """
    if split is None or split.opp <= 0 or overall_rate <= 0:
        return 1.0
    split_rate = regress(getattr(split, attr), split.opp, overall_rate, prior_n)
    return clamp(split_rate / overall_rate, low, high)


def expected_starter_bf(pitcher: Player | None) -> float:
    """Expected batters faced by today's starter, regressed toward a league-average start."""
    if pitcher is None or pitcher.games_pitched <= 0:
        return LEAGUE_BF_PER_START
    if pitcher.games_started / pitcher.games_pitched < 0.7:
        # Mostly a reliever: treat as an opener / bulk-reliever start.
        return clamp(pitcher.pitching.opp / pitcher.games_pitched * 1.5, OPENER_BF, LEAGUE_BF_PER_START)
    bf = (pitcher.pitching.opp + LEAGUE_BF_PER_START * BF_PRIOR_STARTS) / (
        pitcher.games_pitched + BF_PRIOR_STARTS
    )
    return clamp(bf, 12.0, 28.0)


def slot_pa(slot: int | None) -> float:
    return SLOT_PA[slot - 1] if slot and 1 <= slot <= 9 else UNKNOWN_SLOT_PA


def times_facing_starter(slot: int | None, starter_bf: float, total_pa: float) -> float:
    """Expected PAs a hitter in ``slot`` gets against the starter, given the starter faces ``starter_bf``.

    The k-th trip for slot s happens at batter number s + 9k; partial trips are prorated so the
    result moves smoothly with ``starter_bf``.
    """
    if slot is None:
        return min(total_pa, starter_bf / 9.0)
    count = 0.0
    k = 0
    while True:
        position = slot + 9 * k
        portion = clamp(starter_bf - position + 1, 0.0, 1.0)
        if portion <= 0:
            break
        count += portion
        k += 1
    return min(count, total_pa)
