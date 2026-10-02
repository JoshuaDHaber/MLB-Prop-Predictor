"""Historical game data for the backtest: schedules (lineups, weather) and play-by-play outcomes.

Play-by-play responses are large, so each game is reduced to a compact record (one row per
plate appearance) and stored in its own small JSON file. A season is ~2,430 games; the first
run downloads them all, and later runs read from disk.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from mlb_prop_predictor.config import TTL_PAST_SEASON
from mlb_prop_predictor.http import HttpClient, HttpError

log = logging.getLogger(__name__)

BASE = "https://statsapi.mlb.com/api/v1"
PBP_FIELDS = (
    "allPlays,result,eventType,about,halfInning,isComplete,matchup,batter,pitcher,id,batSide,pitchHand,code"
)
FINAL_STATES = {"Final", "Game Over", "Completed Early"}

# Play results that end a half-inning without completing the batter's plate appearance,
# or are administrative entries rather than plate appearances.
_NON_PA_PREFIXES = (
    "caught_stealing",
    "pickoff",
    "stolen_base",
    "wild_pitch",
    "passed_ball",
    "balk",
    "other_advance",
    "runner",
    "game_advisory",
    "os_ruling",
    "ejection",
    "injury",
    "defensive",
    "offensive_sub",
    "pitching_sub",
    "batter_timeout",
    "no_pitch",
    "truncated_pa",
)
STRIKEOUT_EVENTS = {"strikeout", "strikeout_double_play", "strikeout_triple_play"}
HOME_RUN_EVENTS = {"home_run"}


@dataclass
class PA:
    batter: int
    bat_side: str  # side actually batted from: "L" or "R"
    pitcher: int
    pitch_hand: str
    batting: str  # "away" or "home"
    event: str


@dataclass
class HistGame:
    game_pk: int
    date: str
    start_time: str
    away_id: int
    home_id: int
    away_name: str
    home_name: str
    venue_id: int
    roof_type: str | None
    temp_f: float | None
    roof_closed_reported: bool
    lineups: dict[str, list[int]] = field(default_factory=dict)  # "away"/"home" -> batter ids in order
    pas: list[PA] = field(default_factory=list)

    def starter(self, side: str) -> int | None:
        """Starting pitcher for ``side`` = first pitcher the other team batted against."""
        batting = "home" if side == "away" else "away"
        for pa in self.pas:
            if pa.batting == batting:
                return pa.pitcher
        return None

    def lineup(self, side: str) -> list[int]:
        """Posted lineup, or the first nine distinct batters if none was posted."""
        if len(self.lineups.get(side, [])) >= 9:
            return self.lineups[side][:9]
        seen: list[int] = []
        for pa in self.pas:
            if pa.batting == side and pa.batter not in seen:
                seen.append(pa.batter)
            if len(seen) == 9:
                break
        return seen


def is_plate_appearance(event: str) -> bool:
    return bool(event) and not event.startswith(_NON_PA_PREFIXES)


def _code(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("code", ""))
    return str(value or "")


def parse_play_by_play(payload: dict[str, Any]) -> list[PA]:
    pas: list[PA] = []
    for play in payload.get("allPlays", []):
        result = play.get("result") or {}
        about = play.get("about") or {}
        matchup = play.get("matchup") or {}
        event = result.get("eventType") or ""
        if about.get("isComplete") is False or not is_plate_appearance(event):
            continue
        batter = (matchup.get("batter") or {}).get("id")
        pitcher = (matchup.get("pitcher") or {}).get("id")
        if batter is None or pitcher is None:
            continue
        pas.append(
            PA(
                batter=int(batter),
                bat_side=_code(matchup.get("batSide")) or "R",
                pitcher=int(pitcher),
                pitch_hand=_code(matchup.get("pitchHand")) or "R",
                batting="away" if about.get("halfInning") == "top" else "home",
                event=event,
            )
        )
    return pas


def _parse_temp(weather: dict[str, Any]) -> float | None:
    try:
        return float(str(weather.get("temp", "")).strip())
    except ValueError:
        return None


def _month_ranges(season: int, start: date | None, end: date | None) -> Iterator[tuple[date, date]]:
    first = start or date(season, 3, 1)
    last = end or date(season, 11, 15)
    cur = first
    while cur <= last:
        nxt = (cur.replace(day=1) + timedelta(days=32)).replace(day=1)
        yield cur, min(last, nxt - timedelta(days=1))
        cur = nxt


def get_season_games(
    client: HttpClient, season: int, start: date | None = None, end: date | None = None
) -> list[HistGame]:
    """Completed regular-season games, in chronological order (play-by-play not yet attached)."""
    games: list[HistGame] = []
    for lo, hi in _month_ranges(season, start, end):
        payload = client.get(
            f"{BASE}/schedule",
            {
                "sportId": 1,
                "gameType": "R",
                "startDate": lo.isoformat(),
                "endDate": hi.isoformat(),
                "hydrate": "lineups,weather,team,venue(fieldInfo)",
            },
            ttl=TTL_PAST_SEASON,
        ).json()
        for day in payload.get("dates", []):
            for raw in day.get("games", []):
                if (raw.get("status") or {}).get("detailedState") not in FINAL_STATES:
                    continue
                if raw.get("gameType", "R") != "R":
                    continue
                teams = raw["teams"]
                venue = raw.get("venue") or {}
                weather = raw.get("weather") or {}
                lineups = raw.get("lineups") or {}
                condition = str(weather.get("condition", "")).lower()
                games.append(
                    HistGame(
                        game_pk=int(raw["gamePk"]),
                        date=day.get("date") or raw.get("officialDate", raw["gameDate"][:10]),
                        start_time=raw["gameDate"],
                        away_id=int(teams["away"]["team"]["id"]),
                        home_id=int(teams["home"]["team"]["id"]),
                        away_name=teams["away"]["team"].get("name", ""),
                        home_name=teams["home"]["team"].get("name", ""),
                        venue_id=int(venue.get("id", 0)),
                        roof_type=(venue.get("fieldInfo") or {}).get("roofType"),
                        temp_f=_parse_temp(weather),
                        roof_closed_reported="roof closed" in condition or "dome" in condition,
                        lineups={
                            "away": [int(p["id"]) for p in lineups.get("awayPlayers", []) if p.get("id")],
                            "home": [int(p["id"]) for p in lineups.get("homePlayers", []) if p.get("id")],
                        },
                    )
                )
    # Doubleheaders and suspended games can repeat a gamePk; keep the first occurrence.
    seen: set[int] = set()
    unique = []
    for g in sorted(games, key=lambda g: (g.date, g.start_time)):
        if g.game_pk not in seen:
            seen.add(g.game_pk)
            unique.append(g)
    return unique


class PlayByPlayStore:
    """Compact per-game cache of plate appearances."""

    def __init__(self, client: HttpClient, directory: Path | None) -> None:
        self.client = client
        self.directory = directory
        if directory is not None:
            directory.mkdir(parents=True, exist_ok=True)

    def _path(self, game_pk: int) -> Path | None:
        return self.directory / f"{game_pk}.json" if self.directory else None

    def load(self, game_pk: int) -> list[PA] | None:
        path = self._path(game_pk)
        if path is not None and path.exists():
            try:
                return [PA(**row) for row in json.loads(path.read_text(encoding="utf-8"))]
            except (ValueError, TypeError):
                pass
        try:
            payload = self.client.get(f"{BASE}/game/{game_pk}/playByPlay", {"fields": PBP_FIELDS}).json()
            if not payload.get("allPlays"):
                payload = self.client.get(f"{BASE}/game/{game_pk}/playByPlay").json()
        except (HttpError, ValueError) as exc:
            log.warning("play-by-play unavailable for game %s: %s", game_pk, exc)
            return None
        pas = parse_play_by_play(payload)
        if path is not None and pas:
            path.write_text(json.dumps([asdict(p) for p in pas]), encoding="utf-8")
        return pas

    def attach(self, games: Iterable[HistGame], workers: int = 4, progress=None) -> list[HistGame]:
        """Fill ``game.pas`` for each game (in parallel); games without data are dropped."""
        games = list(games)
        done: list[HistGame] = []

        def fetch(g: HistGame) -> tuple[HistGame, list[PA] | None]:
            return g, self.load(g.game_pk)

        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            for i, (g, pas) in enumerate(pool.map(fetch, games), start=1):
                if pas:
                    g.pas = pas
                    done.append(g)
                if progress is not None and (i % 100 == 0 or i == len(games)):
                    progress(i, len(games))
        return done
