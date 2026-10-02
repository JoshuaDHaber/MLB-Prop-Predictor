"""Strikeout distribution for a starting pitcher.

Steps
1. Pitcher K rate per batter faced, regressed (K% stabilizes quickly, ~70 BF),
   with a regressed platoon ratio for each batter's side.
2. Each opposing hitter's K rate per PA vs the pitcher's hand, regressed. If the lineup is
   unknown, the opposing team's season K rate stands in for every slot.
3. log5 per batter, scaled by the park K factor.
4. Batters faced is uncertain, so it is modelled as a normal spread around the pitcher's
   expected workload. For each possible BF the K count is a Poisson-binomial over the lineup
   order (hitter 1, 2, ... 9, 1, ...), and the results are mixed by BF weight.
"""

from __future__ import annotations

from dataclasses import dataclass

from mlb_prop_predictor.domain import Counts, Player
from mlb_prop_predictor.models.common import (
    LeagueRates,
    log5,
    normal_weights,
    poisson_binomial,
    regress,
    scale_probability,
)
from mlb_prop_predictor.models.matchup import batter_hand_vs, expected_starter_bf, platoon_ratio

PITCHER_K_PRIOR = 70
PITCHER_PLATOON_PRIOR = 250
BATTER_K_PRIOR = 60
BATTER_PLATOON_PRIOR = 200
TEAM_K_PRIOR = 1000
BF_SD = 3.5


@dataclass(frozen=True)
class StrikeoutProjection:
    expected_bf: float
    expected_k: float
    pitcher_k_rate: float
    distribution: list[float]  # P(K = n)

    def prob_over(self, line: float) -> float:
        return sum(p for n, p in enumerate(self.distribution) if n > line)

    def prob_under(self, line: float) -> float:
        return sum(p for n, p in enumerate(self.distribution) if n < line)


def pitcher_k_rate(pitcher: Player, lg: LeagueRates) -> float:
    return regress(pitcher.pitching.k, pitcher.pitching.opp, lg.k_per_pa, PITCHER_K_PRIOR)


def batter_k_rate(batter: Player, pitcher_hand: str | None, lg: LeagueRates) -> float:
    overall = regress(batter.hitting.k, batter.hitting.opp, lg.k_per_pa, BATTER_K_PRIOR)
    ratio = platoon_ratio(batter.hitting_vs.get(pitcher_hand or "R"), overall, "k", BATTER_PLATOON_PRIOR)
    return overall * ratio


def project_strikeouts(
    pitcher: Player,
    lineup: list[Player],
    opp_team_batting: Counts | None,
    park_k_factor: float,
    lg: LeagueRates,
) -> StrikeoutProjection:
    base = pitcher_k_rate(pitcher, lg)

    per_slot: list[float] = []
    if lineup:
        for batter in lineup[:9]:
            side = batter_hand_vs(batter, pitcher.pitch_hand)
            p_rate = base * platoon_ratio(pitcher.pitching_vs.get(side), base, "k", PITCHER_PLATOON_PRIOR)
            b_rate = batter_k_rate(batter, pitcher.pitch_hand, lg)
            per_slot.append(scale_probability(log5(b_rate, p_rate, lg.k_per_pa), park_k_factor))
    else:
        team_rate = (
            regress(opp_team_batting.k, opp_team_batting.opp, lg.k_per_pa, TEAM_K_PRIOR)
            if opp_team_batting
            else lg.k_per_pa
        )
        per_slot = [scale_probability(log5(team_rate, base, lg.k_per_pa), park_k_factor)] * 9
    while len(per_slot) < 9:  # short lineup data: pad with the average of what we have
        per_slot.append(sum(per_slot) / len(per_slot))

    mean_bf = expected_starter_bf(pitcher)
    bf_values = [bf for bf in range(int(mean_bf) - 9, int(mean_bf) + 10) if bf >= 3]
    weights = normal_weights(mean_bf, BF_SD, bf_values)

    max_k = max(bf_values) + 1
    mixture = [0.0] * max_k
    for bf, w in zip(bf_values, weights):
        dist = poisson_binomial([per_slot[i % 9] for i in range(bf)])
        for n, p in enumerate(dist):
            mixture[n] += w * p

    expected_k = sum(n * p for n, p in enumerate(mixture))
    return StrikeoutProjection(
        expected_bf=mean_bf,
        expected_k=expected_k,
        pitcher_k_rate=base,
        distribution=mixture,
    )
