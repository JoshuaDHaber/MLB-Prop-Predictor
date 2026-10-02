"""Baseball Savant (Statcast) exit-velocity & barrels leaderboards, downloaded as CSV.

Data is © MLB Advanced Media and is used here for individual, non-commercial purposes.
"""

from __future__ import annotations

import csv
import io
import logging

from mlb_prop_predictor.config import TTL_STATCAST
from mlb_prop_predictor.domain import StatcastLine
from mlb_prop_predictor.http import HttpClient, HttpError

log = logging.getLogger(__name__)

LEADERBOARD_URL = "https://baseballsavant.mlb.com/leaderboard/statcast"


def _num(row: dict[str, str], *names: str) -> float | None:
    for name in names:
        value = row.get(name)
        if value not in (None, "", "null"):
            try:
                return float(value)
            except ValueError:
                continue
    return None


def parse_leaderboard(text: str) -> dict[int, StatcastLine]:
    """Parse the leaderboard CSV. Column names are matched defensively in case Savant renames them."""
    out: dict[int, StatcastLine] = {}
    reader = csv.DictReader(io.StringIO(text))
    for row in reader:
        row = {(k or "").strip().strip('"'): v for k, v in row.items()}
        pid = _num(row, "player_id", "playerid")
        bbe = _num(row, "attempts", "bbe", "batted_balls")
        barrels = _num(row, "barrels", "barrel")
        brl_pa = _num(row, "brl_pa", "barrel_pa", "barrels_per_pa")
        if pid is None or bbe is None or barrels is None or brl_pa is None or brl_pa <= 0:
            continue
        out[int(pid)] = StatcastLine(bbe=int(bbe), barrels=int(barrels), barrels_per_pa=brl_pa / 100.0)
    return out


def get_barrels(
    client: HttpClient, season: int, player_type: str, min_bbe: int = 1
) -> dict[int, StatcastLine]:
    """Barrel data for every ``batter`` or ``pitcher`` with at least ``min_bbe`` batted balls.

    Returns an empty mapping (and logs a warning) if Savant is unreachable, so the
    models fall back to results-based rates instead of failing the run.
    """
    params = {"type": player_type, "year": season, "position": "", "team": "", "min": min_bbe, "csv": "true"}
    try:
        text = client.get(LEADERBOARD_URL, params, ttl=TTL_STATCAST).text
    except HttpError as exc:
        log.warning(
            "Statcast %s leaderboard unavailable (%s); continuing without barrel data", player_type, exc
        )
        return {}
    data = parse_leaderboard(text)
    if not data:
        log.warning(
            "Statcast %s leaderboard returned no usable rows; continuing without barrel data", player_type
        )
    return data
