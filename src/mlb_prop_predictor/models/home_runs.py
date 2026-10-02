"""Probability that a batter hits at least one home run today.

Steps
1. Batter power: blend the batter's regressed HR/PA with a barrel-based estimate
   (barrels/PA x league HR per barrel). Barrel rate stabilizes much faster than HR rate,
   so it carries more weight. Then apply a regressed platoon ratio vs the starter's hand.
2. Pitcher HR suppression: the same idea for HR allowed per batter faced, vs this batter's side.
3. Matchup: combine (1) and (2) with log5 against the league rate.
4. Environment: scale by the park HR factor and a temperature factor.
5. Exposure: expected PAs from the lineup slot, split between the starter (based on his
   typical workload) and the opposing bullpen (team pitching rate).
6. P(at least one HR) = 1 - prod over PAs of (1 - p).
"""

from __future__ import annotations

from dataclasses import dataclass

from mlb_prop_predictor.domain import Counts, Player, StatcastLine
from mlb_prop_predictor.models.common import LeagueRates, clamp, log5, regress, scale_probability
from mlb_prop_predictor.models.matchup import (
    batter_hand_vs,
    expected_starter_bf,
    platoon_ratio,
    slot_pa,
    times_facing_starter,
)

# Regression strengths, in PA / batters faced / implied PA of league-average performance.
BATTER_HR_PRIOR = 200
BATTER_BARREL_PRIOR = 120
BATTER_PLATOON_PRIOR = 1500  # platoon skill is mostly noise; see The Book (Tango et al.)
PITCHER_HR_PRIOR = 1300  # pitcher HR rate stabilizes slowly (~1,300 BF, Carleton)
PITCHER_BARREL_PRIOR = 250
PITCHER_PLATOON_PRIOR = 1500
BULLPEN_PRIOR = 1500

BATTER_BARREL_WEIGHT = 0.55
PITCHER_BARREL_WEIGHT = 0.50


@dataclass(frozen=True)
class HrInputs:
    batter_hr_rate: float  # per PA, after regression/blending/platoon
    pitcher_hr_rate: float  # per BF, after regression/blending/platoon
    p_pa_vs_starter: float
    p_pa_vs_bullpen: float
    pa_vs_starter: float
    pa_total: float


def _barrel_estimate(sc: StatcastLine | None, lg: LeagueRates, prior_n: float) -> float | None:
    if sc is None or sc.barrels_per_pa <= 0:
        return None
    implied_pa = sc.barrels / sc.barrels_per_pa if sc.barrels > 0 else 0.0
    rate = regress(sc.barrels, implied_pa, lg.barrels_per_pa, prior_n)
    return rate * lg.hr_per_barrel


def batter_power(
    batter: Player, statcast: StatcastLine | None, pitcher_hand: str | None, lg: LeagueRates
) -> float:
    overall = regress(batter.hitting.hr, batter.hitting.opp, lg.hr_per_pa, BATTER_HR_PRIOR)
    barrel_based = _barrel_estimate(statcast, lg, BATTER_BARREL_PRIOR)
    skill = (
        overall
        if barrel_based is None
        else (BATTER_BARREL_WEIGHT * barrel_based + (1 - BATTER_BARREL_WEIGHT) * overall)
    )
    ratio = platoon_ratio(batter.hitting_vs.get(pitcher_hand or "R"), overall, "hr", BATTER_PLATOON_PRIOR)
    return skill * ratio


def pitcher_hr_allowed(
    pitcher: Player, statcast: StatcastLine | None, batter_side: str, lg: LeagueRates
) -> float:
    overall = regress(pitcher.pitching.hr, pitcher.pitching.opp, lg.hr_per_pa, PITCHER_HR_PRIOR)
    barrel_based = _barrel_estimate(statcast, lg, PITCHER_BARREL_PRIOR)
    skill = (
        overall
        if barrel_based is None
        else (PITCHER_BARREL_WEIGHT * barrel_based + (1 - PITCHER_BARREL_WEIGHT) * overall)
    )
    ratio = platoon_ratio(pitcher.pitching_vs.get(batter_side), overall, "hr", PITCHER_PLATOON_PRIOR)
    return skill * ratio


def bullpen_hr_rate(team_pitching: Counts | None, lg: LeagueRates) -> float:
    if team_pitching is None:
        return lg.hr_per_pa
    return regress(team_pitching.hr, team_pitching.opp, lg.hr_per_pa, BULLPEN_PRIOR)


def project_home_run(
    batter: Player,
    batter_statcast: StatcastLine | None,
    starter: Player | None,
    starter_statcast: StatcastLine | None,
    opp_team_pitching: Counts | None,
    slot: int | None,
    env_factor: float,
    lg: LeagueRates,
) -> tuple[float, HrInputs]:
    """Return (P(1+ HR), the intermediate inputs for reporting)."""
    p_hand = starter.pitch_hand if starter else None
    side = batter_hand_vs(batter, p_hand)

    b_rate = batter_power(batter, batter_statcast, p_hand, lg)
    sp_rate = pitcher_hr_allowed(starter, starter_statcast, side, lg) if starter else lg.hr_per_pa
    pen_rate = bullpen_hr_rate(opp_team_pitching, lg)

    p_sp = scale_probability(log5(b_rate, sp_rate, lg.hr_per_pa), env_factor)
    p_pen = scale_probability(log5(b_rate, pen_rate, lg.hr_per_pa), env_factor)

    total_pa = slot_pa(slot)
    pa_sp = times_facing_starter(slot, expected_starter_bf(starter), total_pa)
    pa_pen = max(0.0, total_pa - pa_sp)

    p_none = (1 - p_sp) ** pa_sp * (1 - p_pen) ** pa_pen
    prob = clamp(1 - p_none, 0.0, 1.0)
    return prob, HrInputs(b_rate, sp_rate, p_sp, p_pen, pa_sp, total_pa)
