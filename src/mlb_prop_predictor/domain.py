"""Plain data types shared by the sources, models and reports."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class Counts:
    """Opportunities (PA for hitters, batters faced for pitchers) with HR and K outcomes."""

    opp: int = 0
    hr: int = 0
    k: int = 0

    def __add__(self, other: Counts) -> Counts:
        return Counts(self.opp + other.opp, self.hr + other.hr, self.k + other.k)


@dataclass
class Player:
    id: int
    name: str
    bat_side: str | None = None  # "L", "R" or "S"
    pitch_hand: str | None = None  # "L" or "R"
    hitting: Counts = field(default_factory=Counts)
    hitting_vs: dict[str, Counts] = field(default_factory=dict)  # keyed by pitcher hand "L"/"R"
    pitching: Counts = field(default_factory=Counts)
    pitching_vs: dict[str, Counts] = field(default_factory=dict)  # keyed by batter hand "L"/"R"
    games_pitched: int = 0
    games_started: int = 0


@dataclass(frozen=True)
class StatcastLine:
    """Season batted-ball quality from Baseball Savant (for hitters, or allowed by pitchers)."""

    bbe: int
    barrels: int
    barrels_per_pa: float  # fraction, e.g. 0.085


@dataclass(frozen=True)
class Venue:
    id: int
    name: str
    latitude: float | None = None
    longitude: float | None = None
    elevation_ft: float | None = None
    roof_type: str | None = None  # "Open", "Retractable", "Dome"


@dataclass
class TeamSide:
    team_id: int
    name: str  # "New York Yankees"
    short_name: str  # "Yankees"
    probable_pitcher_id: int | None = None
    lineup_ids: list[int] = field(default_factory=list)
    lineup_status: str = "missing"  # "confirmed", "projected" or "missing"


@dataclass
class Game:
    game_pk: int
    start_time: datetime
    status: str
    away: TeamSide
    home: TeamSide
    venue: Venue

    @property
    def label(self) -> str:
        return f"{self.away.short_name} @ {self.home.short_name}"


@dataclass(frozen=True)
class Weather:
    temp_f: float | None
    wind_mph: float | None
    precip_prob: float | None


@dataclass(frozen=True)
class PropPrice:
    bookmaker: str
    side: str  # "Over" / "Under" / "Yes" / "No"
    point: float | None
    price: int  # American odds
