"""Park factors computed from MLB home/road splits, plus a game-time temperature adjustment.

Method (the classic home/road ratio, with shrinkage):

    rate_home = (team hitters' HR + team pitchers' HR allowed) at home / (PA + BF at home)
    rate_road = same, on the road
    raw factor = rate_home / rate_road

Several seasons are pooled, counting only seasons in which the team played in its
current park (teams do move: the Athletics and Rays have both changed homes recently).
The log of the raw factor is shrunk toward 0 (a neutral park) based on sampling noise:

    weight = tau^2 / (tau^2 + 1/events_home + 1/events_road)

where tau is the real-world spread of park effects for that stat.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from mlb_prop_predictor.domain import Counts, Venue, Weather
from mlb_prop_predictor.models.common import clamp

# Typical spread (on the log scale) of true park effects.
TAU_HR = 0.12
TAU_K = 0.04

# Assumed HR sensitivity to air temperature: ~0.8% per degree F, relative to 70F.
HR_TEMP_SENSITIVITY = 0.008
NEUTRAL_TEMP_F = 70.0


@dataclass(frozen=True)
class ParkFactor:
    hr: float = 1.0
    k: float = 1.0
    seasons: int = 0


def _shrunk_ratio(home: Counts, road: Counts, attr: str, tau: float) -> float:
    h_events, r_events = getattr(home, attr), getattr(road, attr)
    if min(h_events, r_events, home.opp, road.opp) <= 0:
        return 1.0
    log_raw = math.log((h_events / home.opp) / (r_events / road.opp))
    noise = 1 / h_events + 1 / r_events
    weight = tau**2 / (tau**2 + noise)
    return math.exp(weight * log_raw)


def compute_park_factors(
    seasons: list[tuple[dict[int, dict[str, Counts]], dict[int, int]]],
    current_venues: dict[int, int],
) -> dict[int, ParkFactor]:
    """Park factors keyed by venue id.

    ``seasons`` is a list of (team home/away counts, team -> venue id) for each season used.
    ``current_venues`` maps each team to its current home venue.
    """
    factors: dict[int, ParkFactor] = {}
    for team_id, venue_id in current_venues.items():
        home, road, used = Counts(), Counts(), 0
        for team_counts, venues in seasons:
            if venues.get(team_id) != venue_id or team_id not in team_counts:
                continue
            c = team_counts[team_id]
            if not all(k in c for k in ("bat_h", "bat_a", "pit_h", "pit_a")):
                continue
            home = home + c["bat_h"] + c["pit_h"]
            road = road + c["bat_a"] + c["pit_a"]
            used += 1
        if used:
            factors[venue_id] = ParkFactor(
                hr=_shrunk_ratio(home, road, "hr", TAU_HR),
                k=_shrunk_ratio(home, road, "k", TAU_K),
                seasons=used,
            )
    return factors


def roof_closed(venue: Venue, weather: Weather) -> bool:
    """Domes are always closed. Retractable roofs are assumed closed in cold, heat or likely rain."""
    roof = (venue.roof_type or "").lower()
    if roof == "dome":
        return True
    if roof != "retractable":
        return False
    if weather.temp_f is None:
        return True
    return weather.temp_f < 60 or weather.temp_f > 88 or (weather.precip_prob or 0) >= 50


def temperature_factor(venue: Venue, weather: Weather) -> float:
    """Multiplier on HR odds from game-time temperature (1.0 indoors or when unknown)."""
    if roof_closed(venue, weather) or weather.temp_f is None:
        return 1.0
    return clamp(math.exp(HR_TEMP_SENSITIVITY * (weather.temp_f - NEUTRAL_TEMP_F)), 0.85, 1.20)
