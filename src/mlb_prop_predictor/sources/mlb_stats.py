"""MLB Stats API (statsapi.mlb.com): schedule, probable starters, lineups, player and team splits.

Data is © MLB Advanced Media and is used here for individual, non-commercial purposes.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Iterable
from datetime import date, datetime, timedelta
from typing import Any

from mlb_prop_predictor.config import TTL_PAST_SEASON, TTL_SCHEDULE, TTL_STATS
from mlb_prop_predictor.domain import Counts, Game, Player, TeamSide, Venue
from mlb_prop_predictor.http import HttpClient, HttpError

log = logging.getLogger(__name__)

BASE = "https://statsapi.mlb.com/api/v1"
SKIP_STATUSES = {"Postponed", "Cancelled", "Suspended"}
_BATCH = 40


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _team_side(side: dict[str, Any], lineup: list[dict[str, Any]]) -> TeamSide:
    team = side.get("team", {})
    pitcher = side.get("probablePitcher") or {}
    ids = [int(p["id"]) for p in lineup if p.get("id") is not None]
    return TeamSide(
        team_id=int(team.get("id", 0)),
        name=team.get("name", ""),
        short_name=team.get("teamName") or team.get("clubName") or team.get("name", ""),
        probable_pitcher_id=int(pitcher["id"]) if pitcher.get("id") else None,
        lineup_ids=ids,
        lineup_status="confirmed" if ids else "missing",
    )


def _venue(raw: dict[str, Any]) -> Venue:
    loc = raw.get("location") or {}
    coords = loc.get("defaultCoordinates") or {}
    field = raw.get("fieldInfo") or {}
    return Venue(
        id=int(raw.get("id", 0)),
        name=raw.get("name", ""),
        latitude=coords.get("latitude"),
        longitude=coords.get("longitude"),
        elevation_ft=loc.get("elevation"),
        roof_type=field.get("roofType"),
    )


def _iter_games(payload: dict[str, Any]) -> Iterable[dict[str, Any]]:
    for day in payload.get("dates", []):
        yield from day.get("games", [])


def get_schedule(client: HttpClient, day: str) -> list[Game]:
    """Games on ``day`` (YYYY-MM-DD) with probable pitchers, posted lineups and venue details."""
    payload = client.get(
        f"{BASE}/schedule",
        {
            "sportId": 1,
            "date": day,
            "hydrate": "probablePitcher,lineups,team,venue(location,fieldInfo)",
        },
        ttl=TTL_SCHEDULE,
    ).json()
    games: list[Game] = []
    for raw in _iter_games(payload):
        status = (raw.get("status") or {}).get("detailedState", "")
        if status in SKIP_STATUSES:
            continue
        lineups = raw.get("lineups") or {}
        games.append(
            Game(
                game_pk=int(raw["gamePk"]),
                start_time=_parse_time(raw["gameDate"]),
                status=status,
                away=_team_side(raw["teams"]["away"], lineups.get("awayPlayers", [])),
                home=_team_side(raw["teams"]["home"], lineups.get("homePlayers", [])),
                venue=_venue(raw.get("venue") or {}),
            )
        )
    return games


def fill_projected_lineups(client: HttpClient, games: list[Game], day: str, lookback_days: int = 5) -> None:
    """For teams without a posted lineup, use their most recent posted lineup as a projection."""
    missing = {side.team_id for g in games for side in (g.away, g.home) if not side.lineup_ids}
    if not missing:
        return
    end = date.fromisoformat(day) - timedelta(days=1)
    start = end - timedelta(days=lookback_days - 1)
    payload = client.get(
        f"{BASE}/schedule",
        {"sportId": 1, "startDate": start.isoformat(), "endDate": end.isoformat(), "hydrate": "lineups"},
        ttl=TTL_STATS,
    ).json()
    latest: dict[int, tuple[datetime, list[int]]] = {}
    for raw in _iter_games(payload):
        lineups = raw.get("lineups") or {}
        when = _parse_time(raw["gameDate"])
        for side_key, lineup_key in (("away", "awayPlayers"), ("home", "homePlayers")):
            team_id = int(raw["teams"][side_key]["team"]["id"])
            ids = [int(p["id"]) for p in lineups.get(lineup_key, []) if p.get("id")]
            if team_id in missing and ids and (team_id not in latest or when > latest[team_id][0]):
                latest[team_id] = (when, ids)
    for g in games:
        for side in (g.away, g.home):
            if not side.lineup_ids and side.team_id in latest:
                side.lineup_ids = latest[side.team_id][1]
                side.lineup_status = "projected"


# -- player stats -----------------------------------------------------------


def _hitting_counts(stat: dict[str, Any]) -> Counts:
    pa = stat.get("plateAppearances")
    if pa is None:
        pa = sum(
            int(stat.get(k, 0) or 0) for k in ("atBats", "baseOnBalls", "hitByPitch", "sacFlies", "sacBunts")
        )
    return Counts(int(pa or 0), int(stat.get("homeRuns", 0) or 0), int(stat.get("strikeOuts", 0) or 0))


def _pitching_counts(stat: dict[str, Any]) -> Counts:
    return Counts(
        int(stat.get("battersFaced", 0) or 0),
        int(stat.get("homeRuns", 0) or 0),
        int(stat.get("strikeOuts", 0) or 0),
    )


def _combine_splits(splits: list[dict[str, Any]], counter) -> Counts:
    """Season totals for one split code.

    Traded players get one row per team plus (usually) a combined row with no ``team``;
    prefer the combined row, otherwise add the team rows together.
    """
    combined = [s for s in splits if "team" not in s]
    rows = combined or splits
    total = Counts()
    for row in rows:
        total = total + counter(row.get("stat", {}))
    return total


def _games_pitched(stat: dict[str, Any]) -> int:
    return int(stat.get("gamesPitched", stat.get("gamesPlayed", 0)) or 0)


def _split_code(row: dict[str, Any]) -> str:
    split = row.get("split")
    if isinstance(split, dict):
        return str(split.get("code", "")).lower()
    return ""


def _apply_stats(player: Player, stats: list[dict[str, Any]]) -> None:
    for block in stats:
        kind = (block.get("type") or {}).get("displayName")
        group = (block.get("group") or {}).get("displayName")
        splits = block.get("splits") or []
        counter = _hitting_counts if group == "hitting" else _pitching_counts
        if kind == "season":
            total = _combine_splits(splits, counter)
            if group == "hitting":
                player.hitting = total
            elif group == "pitching":
                player.pitching = total
                rows = [s for s in splits if "team" not in s] or splits
                player.games_pitched = sum(_games_pitched(s.get("stat", {})) for s in rows)
                player.games_started = sum(int(s.get("stat", {}).get("gamesStarted", 0) or 0) for s in rows)
        elif kind == "statSplits":
            by_code: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for row in splits:
                by_code[_split_code(row)].append(row)
            target = player.hitting_vs if group == "hitting" else player.pitching_vs
            for code, hand in (("vl", "L"), ("vr", "R")):
                if by_code.get(code):
                    target[hand] = _combine_splits(by_code[code], counter)


def _people_batch(client: HttpClient, ids: list[int], season: int, group: str) -> list[dict[str, Any]]:
    hydrate = f"stats(group=[{group}],type=[season,statSplits],sitCodes=[vl,vr],season={season})"
    payload = client.get(
        f"{BASE}/people",
        {"personIds": ",".join(str(i) for i in ids), "hydrate": hydrate},
        ttl=TTL_STATS,
    ).json()
    return payload.get("people", [])


def get_players(
    client: HttpClient, hitter_ids: Iterable[int], pitcher_ids: Iterable[int], season: int
) -> dict[int, Player]:
    """Handedness plus season and platoon-split counts for every hitter and pitcher requested."""
    players: dict[int, Player] = {}

    def ensure(raw: dict[str, Any]) -> Player:
        pid = int(raw["id"])
        if pid not in players:
            players[pid] = Player(
                id=pid,
                name=raw.get("fullName", str(pid)),
                bat_side=(raw.get("batSide") or {}).get("code"),
                pitch_hand=(raw.get("pitchHand") or {}).get("code"),
            )
        return players[pid]

    for group, ids in (("hitting", sorted(set(hitter_ids))), ("pitching", sorted(set(pitcher_ids)))):
        for i in range(0, len(ids), _BATCH):
            for raw in _people_batch(client, ids[i : i + _BATCH], season, group):
                _apply_stats(ensure(raw), raw.get("stats", []))

    # The batched hydrate occasionally omits pitching platoon splits; fetch those one by one.
    for pid in set(pitcher_ids):
        p = players.get(pid)
        if p is not None and not p.pitching_vs:
            payload = client.get(
                f"{BASE}/people/{pid}/stats",
                {"stats": "statSplits", "group": "pitching", "season": season, "sitCodes": "vl,vr"},
                ttl=TTL_STATS,
            ).json()
            for block in payload.get("stats", []):
                block.setdefault("type", {"displayName": "statSplits"})
                block.setdefault("group", {"displayName": "pitching"})
            _apply_stats(p, payload.get("stats", []))
    return players


# -- team stats -------------------------------------------------------------


def _collect_home_away(
    payload: dict[str, Any], out: dict[int, dict[str, Counts]], team_id: int | None
) -> None:
    for block in payload.get("stats", []):
        group = (block.get("group") or {}).get("displayName")
        for row in block.get("splits", []):
            tid = (row.get("team") or {}).get("id", team_id)
            code = _split_code(row)
            if tid is None or code not in ("h", "a") or group not in ("hitting", "pitching"):
                continue
            prefix, counter = ("bat", _hitting_counts) if group == "hitting" else ("pit", _pitching_counts)
            out[int(tid)][f"{prefix}_{code}"] = counter(row.get("stat", {}))


def get_team_home_away(
    client: HttpClient, season: int, current_season: int, team_ids: Iterable[int] = ()
) -> dict[int, dict[str, Counts]]:
    """Per-team home/away counts for one season.

    Keys: ``bat_h``, ``bat_a`` (the team's hitters) and ``pit_h``, ``pit_a`` (its pitchers).
    Uses the league-wide endpoint, then fills any team in ``team_ids`` it missed one by one.
    """
    ttl = TTL_STATS if season >= current_season else TTL_PAST_SEASON
    out: dict[int, dict[str, Counts]] = defaultdict(dict)
    for group in ("hitting", "pitching"):
        payload = client.get(
            f"{BASE}/teams/stats",
            {"season": season, "group": group, "stats": "statSplits", "sitCodes": "h,a", "sportIds": 1},
            ttl=ttl,
        ).json()
        for block in payload.get("stats", []):
            block.setdefault("group", {"displayName": group})
        _collect_home_away(payload, out, None)

    needed = ("bat_h", "bat_a", "pit_h", "pit_a")
    for tid in team_ids:
        if all(k in out.get(tid, {}) for k in needed):
            continue
        try:
            payload = client.get(
                f"{BASE}/teams/{tid}/stats",
                {"season": season, "group": "hitting,pitching", "stats": "statSplits", "sitCodes": "h,a"},
                ttl=ttl,
            ).json()
        except HttpError as exc:
            log.warning("home/away splits unavailable for team %s in %s: %s", tid, season, exc)
            continue
        _collect_home_away(payload, out, tid)
    return dict(out)


def get_team_venues(client: HttpClient, season: int, current_season: int) -> dict[int, int]:
    """Home venue id for each MLB team in ``season`` (teams occasionally move parks)."""
    ttl = TTL_STATS if season >= current_season else TTL_PAST_SEASON
    payload = client.get(f"{BASE}/teams", {"sportId": 1, "season": season}, ttl=ttl).json()
    return {
        int(t["id"]): int(t["venue"]["id"])
        for t in payload.get("teams", [])
        if (t.get("venue") or {}).get("id") is not None
    }
